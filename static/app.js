'use strict';

const $ = id => document.getElementById(id);
const sectionNames = {overview:'项目概览', forecast:'1 小时预报', comparison:'空间天气对比', live:'机场数据', methods:'模型与口径'};
const modelNames = {
  seasonal:'模型 A · 仅时间规律',
  baseline:'模型 A · 仅时间规律',
  space:'模型 B · 时间规律＋空间天气'
};
let meta = null;
let currentQuery = '';
let historyRequest = 0;
let weatherPending = false;
let lastWeatherCheck = 0;
let comparisonPending = null;
let comparisonLoaded = false;

const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
const fmt = (value, digits=1) => finite(value)
  ? Number(value).toLocaleString('zh-CN', {minimumFractionDigits:digits, maximumFractionDigits:digits})
  : '—';
const pct = value => finite(value) ? fmt(Number(value) * 100, 1) + '%' : '—';
const pickNumber = (...values) => {
  const value = values.find(finite);
  return finite(value) ? Number(value) : null;
};
const signed = (value, digits=1, suffix='') => finite(value)
  ? `${Number(value) > 0 ? '+' : ''}${fmt(value, digits)}${suffix}`
  : '—';
const percentValue = value => {
  if (!finite(value)) return null;
  const number = Number(value);
  return Math.abs(number) <= 1 ? number * 100 : number;
};
const percentagePoints = value => {
  if (!finite(value)) return null;
  const number = Number(value);
  return Math.abs(number) <= 1 ? number * 100 : number;
};
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
  '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
}[char]));
const localTime = value => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? esc(value)
    : date.toLocaleString('zh-CN', {timeZone:'Asia/Shanghai', hour12:false});
};
const historicalTime = value => esc(String(value ?? '').replace('T', ' ').slice(0, 16));
const modelLabel = name => modelNames[name] || name || '未知模型';

function toast(message) {
  $('toast').textContent = message;
  $('toast').hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => $('toast').hidden = true, 5200);
}

async function api(path, body) {
  const options = body === undefined ? {} : {
    method:'POST',
    headers:{'Content-Type':'application/json', 'X-CSRF-Token':meta?.csrf ?? ''},
    body:JSON.stringify(body)
  };
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error('暂时无法连接网站，请检查网络后重试。');
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error('服务响应无法读取，请稍后刷新页面。');
  }
  if (response.status === 401) {
    location.replace('/login');
    throw new Error('登录状态已失效，请重新登录。');
  }
  if (!response.ok) throw new Error(data.error || '请求失败，请稍后重试。');
  return data;
}

async function busy(button, work) {
  const html = button.innerHTML;
  button.disabled = true;
  button.textContent = '处理中…';
  try {
    await work();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.innerHTML = html;
  }
}

function tab(name, options={}) {
  if (!sectionNames[name]) return;
  const page = $(name);
  document.querySelectorAll('.page').forEach(item => item.classList.toggle('active', item.id === name));
  document.querySelectorAll('.nav[data-tab]').forEach(item => {
    const active = item.dataset.tab === name;
    item.classList.toggle('active', active);
    if (active) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  $('current-section').textContent = sectionNames[name];
  document.title = sectionNames[name] + ' · 航迹';
  if (options.updateUrl !== false && location.hash !== '#' + name) history.pushState({tab:name}, '', '#' + name);
  window.scrollTo({top:0, behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  if (options.focus !== false) page.focus({preventScroll:true});
  if (name === 'comparison' && meta && !comparisonLoaded && !comparisonPending) {
    loadSpaceComparison().catch(error => console.warn(error));
  }
}

document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => tab(button.dataset.tab)));
document.querySelectorAll('[data-jump]').forEach(button => button.addEventListener('click', () => tab(button.dataset.jump)));
window.addEventListener('popstate', () => tab(location.hash.slice(1) || 'overview', {updateUrl:false, focus:false}));
$('go-forecast').addEventListener('click', () => tab('forecast'));
$('logout-button').addEventListener('click', event => busy(event.currentTarget, async () => {
  await api('/api/auth/logout', {});
  location.replace('/login');
}));

function stat(label, value, unit, note, featured=false) {
  return `<div class="stat ${featured ? 'featured' : ''}"><div class="stat-label">${esc(label)}<span aria-hidden="true">↗</span></div><div class="stat-value">${value}<small>${esc(unit)}</small></div><div class="stat-note">${esc(note)}</div></div>`;
}

function comparisonGroup(group, label, tone) {
  const flights = pickNumber(group?.flight_count, group?.flights);
  const mean = pickNumber(group?.mean_delay_minutes, group?.actual_mean_delay, group?.mean_delay);
  const days = pickNumber(group?.actual_departure_day_count, group?.day_count);
  const median = pickNumber(group?.median_delay_minutes, group?.median_delay);
  const p90 = pickNumber(group?.p90_delay_minutes, group?.p90_delay);
  const over30 = pickNumber(group?.arrival_delay_over_30_rate, group?.over_30_rate);
  return `<div class="group-card-head"><div><span class="group-dot ${tone}"></span><span>${esc(label)}</span></div><small>${fmt(flights,0)} 个有效首段</small></div>` +
    `<div class="group-delay"><strong>${fmt(mean)}</strong><span>分钟</span><small>首段平均到港延误</small></div>` +
    `<div class="group-detail-grid"><div><small>实际离港自然日</small><b>${fmt(days,0)} 天</b></div><div><small>到港延误 ≥30 min</small><b>${pct(over30)}</b></div><div><small>到港延误中位数</small><b>${fmt(median)} min</b></div><div><small>到港延误 P90</small><b>${fmt(p90)} min</b></div></div>`;
}

function renderSpaceComparison(data) {
  comparisonLoaded = true;
  const paper = data.paper_benchmark ?? data.paper ?? {};
  const paperDelay = paper.mean_arrival_delay_minutes ?? paper.arrival_delay_minutes ?? {};
  const paperRate = paper.arrival_delay_over_30_rate ?? paper.over_30_rate ?? {};
  const paperCalmMean = pickNumber(paperDelay.calm, paperDelay.quiet, paper.calm_mean_delay_minutes, paper.calm_mean, 9.11);
  const paperActiveMean = pickNumber(paperDelay.active, paperDelay.event, paper.active_mean_delay_minutes, paper.active_mean, 16.52);
  const paperMeanDiff = pickNumber(paperDelay.difference, paperDelay.difference_minutes, paper.mean_delay_difference_minutes, paperActiveMean - paperCalmMean);
  const paperRelative = percentValue(pickNumber(paperDelay.relative_increase, paperDelay.relative_increase_percent, paper.relative_increase, 81.34));
  const paperCalmRate = percentValue(pickNumber(paperRate.calm, paperRate.quiet, paper.calm_over_30_rate, .1776));
  const paperActiveRate = percentValue(pickNumber(paperRate.active, paperRate.event, paper.active_over_30_rate, .2157));
  const paperRateDiff = percentagePoints(pickNumber(paperRate.difference_percentage_points, paperRate.difference, paper.over_30_difference_percentage_points, 3.81));
  $('paper-comparison').innerHTML =
    `<div class="paper-metric"><div class="metric-title"><span>平均到达延误</span><small>全部样本的均值</small></div><div class="metric-pair"><div class="metric-side"><small>相对平静期</small><strong>${fmt(paperCalmMean,2)}</strong><span>分钟</span></div><div class="metric-arrow"><b>${signed(paperMeanDiff,2)}</b><small>${signed(paperRelative,2,'%')}</small><span aria-hidden="true">→</span></div><div class="metric-side active"><small>空间天气事件期</small><strong>${fmt(paperActiveMean,2)}</strong><span>分钟</span></div></div></div>` +
    `<div class="paper-metric"><div class="metric-title"><span>到达延误超过 30 分钟</span><small>论文与本站首段到港对比均采用 ≥30 分钟阈值</small></div><div class="metric-pair"><div class="metric-side"><small>相对平静期</small><strong>${fmt(paperCalmRate,2)}%</strong><span>航班占比</span></div><div class="metric-arrow"><b>${signed(paperRateDiff,2,' pp')}</b><small>百分点变化</small><span aria-hidden="true">→</span></div><div class="metric-side active"><small>空间天气事件期</small><strong>${fmt(paperActiveRate,2)}%</strong><span>航班占比</span></div></div></div>`;

  const classification = data.classification ?? data.grouping ?? {};
  const groups = data.groups ?? {};
  const calm = groups.calm ?? groups.quiet ?? {};
  const active = groups.active ?? groups.event ?? {};
  const difference = data.difference ?? data.differences ?? {};
  const validFlights = pickNumber(classification.valid_first_leg_arrivals);
  const excludedFlights = pickNumber(classification.excluded_boundary_or_partial_day_flights);
  const eventRows = pickNumber(classification.event_rows_in_local_period);
  const eventClusters = pickNumber(classification.independent_event_clusters);
  const quietFlights = pickNumber(calm.flight_count, classification.quiet_flights);
  const eventFlights = pickNumber(active.flight_count, classification.event_flights);
  const meanDifference = difference.mean_arrival_delay_minutes ?? {};
  const rawDiff = pickNumber(meanDifference.point, difference.mean_arrival_delay_minutes);
  const rateDiff = pickNumber(difference.arrival_delay_over_30_percentage_points);
  const ks = difference.distribution_test ?? {};

  const adjusted = data.calendar_adjusted_event_cluster ?? {};
  const adjustedDelay = adjusted.mean_arrival_delay_minutes ?? {};
  const adjustedDiff = pickNumber(adjustedDelay.point);
  const adjustedCi = adjustedDelay.ci95 ?? {};
  const adjustedCiLow = pickNumber(adjustedCi.lower);
  const adjustedCiHigh = pickNumber(adjustedCi.upper);
  const positiveClusters = pickNumber(adjustedDelay.positive_event_clusters);
  const adjustedRate = adjusted.arrival_delay_over_30_percentage_points ?? {};
  const adjustedRateDiff = pickNumber(adjustedRate.point);
  const robust = Boolean(adjusted.robust_direction_supported);

  $('comparison-status').classList.remove('error');
  $('comparison-status').textContent = `2017—2019 PEK 始发首段 · 论文附录 ${fmt(eventRows,0)} 条事件 · 合并为 ${fmt(eventClusters,0)} 个事件簇 · 边界/残余期排除 ${fmt(excludedFlights,0)} 个首段`;
  $('comparison-stats').innerHTML =
    stat('有效首段到港', fmt(validFlights,0), '个', '每次 PEK 物理起飞只取第一停靠站') +
    stat('安静期 / 事件期', `${fmt(quietFlights,0)} / ${fmt(eventFlights,0)}`, '个', '事件期样本不与安静期强行等量') +
    stat('原始平均到港延误差', signed(rawDiff), '分钟', '未校正 · 航班记录等权', true) +
    stat('事件簇调整后 95% 区间', finite(adjustedCiLow) && finite(adjustedCiHigh) ? `${fmt(adjustedCiLow,1)}～${fmt(adjustedCiHigh,1)}` : '—', '分钟', `${fmt(positiveClusters,0)} / ${fmt(eventClusters,0)} 个事件簇为正`);
  $('calm-summary').innerHTML = comparisonGroup(calm, '完整安静期', 'calm');
  $('active-summary').innerHTML = comparisonGroup(active, '空间天气事件期', 'active');

  const calmMean = pickNumber(calm.mean_delay_minutes, calm.actual_mean_delay, calm.mean_delay);
  const activeMean = pickNumber(active.mean_delay_minutes, active.actual_mean_delay, active.mean_delay);
  const calmRate = pickNumber(calm.arrival_delay_over_30_rate);
  const activeRate = pickNumber(active.arrival_delay_over_30_rate);
  const ksP = ks.p_value_text ?? (finite(ks.p_value) ? Number(ks.p_value).toExponential(2) : '—');
  $('comparison-rows').innerHTML =
    `<tr><td><strong>首段平均到港延误</strong><br><small>首段航班记录等权</small></td><td>${fmt(calmMean)} min</td><td>${fmt(activeMean)} min</td><td class="${rawDiff > 0 ? 'delay' : 'early'}">${signed(rawDiff)} min</td></tr>` +
    `<tr><td><strong>首段到港延误 ≥30 分钟</strong><br><small>与论文阈值一致</small></td><td>${pct(calmRate)}</td><td>${pct(activeRate)}</td><td>${signed(rateDiff,2,' pp')}</td></tr>` +
    `<tr><td><strong>航班记录分布 KS 检验</strong><br><small>只说明经验分布可区分</small></td><td colspan="2">双侧 D = ${fmt(ks.statistic,4)}</td><td>p = ${esc(ksP)}<br><small>不是事件层稳健性证据</small></td></tr>` +
    `<tr><td><strong>日历调整后的事件簇等权差</strong><br><small>年 × 月 × 星期 × 计划到港小时；28 簇等权</small></td><td colspan="2">调整后 ≥30 min 差 ${signed(adjustedRateDiff,2,' pp')}</td><td class="${adjustedDiff > 0 ? 'delay' : 'early'}">${signed(adjustedDiff)} min<br><small>95% 区间 ${fmt(adjustedCiLow,1)}～${fmt(adjustedCiHigh,1)}</small></td></tr>`;
  const scoreNote = classification.method ?? '论文 Table A 事件期与完整无事件自然日对比';
  $('comparison-threshold-note').innerHTML = `<strong>分组方法：</strong>${esc(scoreNote)}。事件样本与安静样本不强行等量；事件日残余时段不归入安静期。首段到港结果表示 PEK 始发队列的下游表现，不是到达 PEK 的入港延误。`;

  const model = data.model_comparison ?? data.models ?? {};
  const validation = model.validation ?? {};
  const test = model.test ?? model.independent_test ?? {};
  const validationA = pickNumber(validation.model_a_mae, validation.baseline_mae, validation.calendar_mae, validation.baseline?.mae, meta?.validation?.baseline?.mae);
  const validationB = pickNumber(validation.model_b_mae, validation.space_mae, validation.space_weather_mae, validation.space?.mae, meta?.validation?.space?.mae);
  const testA = pickNumber(test.model_a_mae, test.baseline_mae, test.calendar_mae, test.baseline?.mae, model.baseline_test_mae, meta?.baseline_model_test?.mae);
  const testB = pickNumber(test.model_b_mae, test.space_mae, test.space_weather_mae, test.space?.mae, model.space_test_mae, meta?.space_model_test?.mae);
  const validationGain = finite(validation.b_relative_improvement_percent)
    ? Number(validation.b_relative_improvement_percent)
    : percentValue(pickNumber(validation.relative_improvement, validation.improvement_percent, model.validation_improvement));
  const testGain = finite(test.b_relative_improvement_percent)
    ? Number(test.b_relative_improvement_percent)
    : percentValue(pickNumber(test.relative_improvement, test.improvement_percent, model.test_improvement));
  const threshold = finite(model.enable_threshold_relative_percent)
    ? Number(model.enable_threshold_relative_percent)
    : percentValue(pickNumber(model.enable_threshold, model.enable_threshold_percent, .01));
  const enabled = Boolean(model.space_enabled ?? model.space_model_enabled ?? model.enabled ?? false);
  const modelConclusion = model.conclusion ?? '模型 B 的历史开发结果有小幅改善，但历史留出、时区确认和全新确认段等生产证据尚未闭合，因此当前主预报使用模型 A。';
  const validationBootstrap = validation.bootstrap ?? model.validation_bootstrap ?? {};
  const testBootstrap = test.bootstrap ?? model.test_bootstrap ?? {};
  const ciText = detail => finite(detail.lower) && finite(detail.upper)
    ? `B-A 95% 区间 ${signed(detail.lower,3)}～${signed(detail.upper,3)} min`
    : '';
  const gates = model.production_gates ?? {};
  const gateSummary = [
    ['历史开发', gates.internal_screening_pass],
    ['2019 留出', gates.historical_holdout_screen_pass],
    ['ROTI 时区', gates.roti_timezone_confirmed_utc],
    ['全新确认段', gates.fresh_confirmation_available]
  ].map(([label, passed]) => `${label} ${passed ? '✓' : '×'}`).join(' · ');
  $('model-comparison-rows').innerHTML =
    `<tr><td><strong>${esc(validation.stage_label ?? '2018 Q3 历史开发评估')}</strong><br><small>内部筛查 · 改善门槛 ${fmt(threshold,0)}%</small></td><td>${fmt(validationA,2)} min</td><td>${fmt(validationB,2)} min</td><td>${signed(validationGain,2,'%')}<br><small>${esc(ciText(validationBootstrap))}</small></td></tr>` +
    `<tr><td><strong>${esc(test.stage_label ?? '2019 历史留出审计')}</strong><br><small>只作历史稳定性审计</small></td><td>${fmt(testA,2)} min</td><td>${fmt(testB,2)} min</td><td>${signed(testGain,2,'%')}<br><small>${esc(ciText(testBootstrap))}</small></td></tr>`;
  $('space-model-verdict').className = `pill ${enabled ? 'teal' : 'amber'}`;
  $('space-model-verdict').textContent = enabled ? '当前启用模型 B' : '当前启用模型 A';
  $('model-comparison-conclusion').innerHTML = `<strong>${enabled ? '当前结论：模型 B 已通过全部生产门槛。' : '当前结论：空间天气存在研究信号，但尚不能生产启用。'}</strong><span>${esc(modelConclusion)}</span><small>${esc(gateSummary)}</small>`;

  $('comparison-interpretation').innerHTML = `事件期首段平均到港延误比完整安静期高 <strong>${signed(rawDiff)} 分钟</strong>，到港延误 ≥30 分钟的比例高 <strong>${signed(rateDiff,2,' pp')}</strong>；航班记录级 KS 检验显示两组经验分布可以区分。按年、月、星期与计划到港小时建立安静参照，并让 ${fmt(eventClusters,0)} 个事件簇等权后，差异为 <strong>${signed(adjustedDiff)} 分钟</strong>，95% 区间为 ${fmt(adjustedCiLow,1)}～${fmt(adjustedCiHigh,1)} 分钟，${robust ? '区间未跨 0' : '区间跨过 0'}。因此当前结论是：<strong>${robust ? '存在历史关联信号，但仍不是因果或预测增益证明。' : '航班记录层面有差异，事件层证据尚不稳定，不能宣称稳健显著。'}</strong>`;
  const limitations = data.limitations ?? [];
  $('comparison-limitations').innerHTML = (limitations.length ? limitations : [
    '总体差异不意味着每次空间天气事件都会增加延误。',
    '结果不能直接用于判断某个航班、某条航线或某架飞机。',
    '地面天气、流量和运行保障等混杂因素仍需继续控制。'
  ]).map(item => `<li>${esc(typeof item === 'string' ? item : item.text ?? item.note ?? String(item))}</li>`).join('');
}

async function loadSpaceComparison() {
  if (comparisonPending) return comparisonPending;
  comparisonPending = (async () => {
    try {
      const data = await api('/api/space-comparison');
      renderSpaceComparison(data);
      return data;
    } catch (error) {
      $('comparison-status').textContent = '对比数据暂时无法读取：' + error.message;
      $('comparison-status').classList.add('error');
      throw error;
    } finally {
      comparisonPending = null;
    }
  })();
  return comparisonPending;
}

function thin(points, limit=320) {
  if (points.length <= limit) return points;
  const step = Math.ceil(points.length / limit);
  return points.filter((_, index) => index % step === 0 || index === points.length - 1);
}

function chart(container, rawSeries, label) {
  const series = rawSeries.map(item => ({...item, points:thin(item.points)}));
  const valid = series.flatMap(item => item.points.filter(point => Number.isFinite(point.y)));
  if (!valid.length) {
    container.innerHTML = '<div class="empty-state"><p>该时间段暂无有效机场小时记录。</p></div>';
    return;
  }
  const width = 760, height = 270, padding = {l:46, r:20, t:25, b:40};
  const allDates = [...new Set(valid.map(point => point.x))].sort();
  const timestamps = allDates.map(value => Date.parse(value));
  const minTime = Math.min(...timestamps), maxTime = Math.max(...timestamps);
  let minY = Math.min(0, ...valid.map(point => point.y));
  let maxY = Math.max(10, ...valid.map(point => point.y));
  const yPadding = Math.max(4, (maxY - minY) * .12);
  minY -= yPadding;
  maxY += yPadding;
  const x = value => minTime === maxTime
    ? (padding.l + width - padding.r) / 2
    : padding.l + ((Date.parse(value) - minTime) / (maxTime - minTime)) * (width - padding.l - padding.r);
  const y = value => height - padding.b - (value - minY) / (maxY - minY) * (height - padding.t - padding.b);
  let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title>`;
  for (let index=0; index<=4; index++) {
    const value = minY + (maxY - minY) * index / 4;
    const rowY = y(value);
    svg += `<line x1="${padding.l}" y1="${rowY}" x2="${width-padding.r}" y2="${rowY}" stroke="#e7eeeb"/><text x="${padding.l-10}" y="${rowY+3}" text-anchor="end" fill="#81928f" font-size="10">${value.toFixed(0)}</text>`;
  }
  const every = Math.max(1, Math.ceil(allDates.length / 6));
  allDates.forEach((value, index) => {
    if (index % every === 0 || index === allDates.length - 1) {
      const labelText = String(value).slice(5,13).replace('T',' ');
      svg += `<text x="${x(value)}" y="${height-13}" text-anchor="middle" fill="#81928f" font-size="10">${esc(labelText)}</text>`;
    }
  });
  for (const item of series) {
    let path = '', last = null;
    for (const point of item.points) {
      if (!Number.isFinite(point.y)) { last = null; continue; }
      const gap = last && Date.parse(point.x) - Date.parse(last) > 7200000;
      path += `${!last || gap ? 'M' : 'L'}${x(point.x).toFixed(1)},${y(point.y).toFixed(1)} `;
      last = point.x;
    }
    svg += `<path d="${path}" fill="none" stroke="${item.color}" stroke-width="2.2" ${item.dash ? 'stroke-dasharray="6 5"' : ''} stroke-linecap="round" stroke-linejoin="round"/>`;
    for (const point of item.points) {
      if (!Number.isFinite(point.y)) continue;
      const accessible = `${item.name}，${historicalTime(point.x)}，${fmt(point.y)} 分钟`;
      svg += `<circle tabindex="0" role="img" aria-label="${esc(accessible)}" cx="${x(point.x)}" cy="${y(point.y)}" r="${allDates.length > 70 ? 1.7 : 3.2}" fill="${item.color}" stroke="white" stroke-width="1"><title>${esc(accessible)}</title></circle>`;
    }
  }
  container.innerHTML = svg + '</svg>';
}

function validateHistory() {
  if (!$('start').value || !$('end').value) throw new Error('请选择完整的开始和结束日期。');
  if ($('start').value > $('end').value) throw new Error('开始日期不能晚于结束日期。');
}

async function loadHistory() {
  validateHistory();
  const requestId = ++historyRequest;
  const query = new URLSearchParams({airport:$('airport').value, start:$('start').value, end:$('end').value});
  $('history-status').classList.remove('error');
  $('history-status').textContent = '正在查询机场小时窗口…';
  $('export').disabled = true;
  try {
    const data = await api('/api/backtest?' + query);
    if (requestId !== historyRequest) return;
    currentQuery = query.toString();
    $('export').disabled = false;
    const summary = data.summary;
    $('history-status').textContent = `${data.start} — ${data.end} · 2019 历史留出数据 · ${modelLabel(data.selected_model)} · 查询完成`;
    $('stats').innerHTML =
      stat('有效机场小时', fmt(summary.window_count, 0), '个', '每个窗口等权') +
      stat('覆盖有效航班', fmt(summary.flight_count, 0), '班', `平均 ${fmt(summary.mean_flights_per_window)} 班 / 窗口`) +
      stat('实际小时平均延误', fmt(summary.actual_mean), '分钟', '窗口均值的算术平均', true) +
      stat('1 小时窗口 MAE', fmt(summary.mae), '分钟', `RMSE ${fmt(summary.rmse)} · Bias ${fmt(summary.bias)}`);
    const points = data.series;
    chart($('history-chart'), [
      {name:'实际小时均值', color:'#2b8275', points:points.map(point => ({x:point.window_start, y:Number(point.actual_mean_delay)}))},
      {name:'启用预报', color:'#d28a48', dash:true, points:points.map(point => ({x:point.window_start, y:Number(point.predicted_mean_delay)}))},
      {name:'星期×时段基线', color:'#9aa8bb', points:points.map(point => ({x:point.window_start, y:Number(point.simple_baseline_mean_delay)}))}
    ], 'PEK 一小时平均离港延误：实际、启用预报与历史时段基线');
    $('chart-description').textContent = `PEK · ${fmt(summary.window_count,0)} 个一小时窗口 · 实际均值超过 15 分钟的窗口占 ${pct(summary.hours_over_15)}`;
    $('table-caption').textContent = `共 ${fmt(data.record_total,0)} 个机场小时，页面显示前 ${Math.min(data.record_total,data.record_limit)} 个；下载包含全部筛选窗口。`;
    $('history-rows').innerHTML = data.records.length ? data.records.map(row => {
      const className = row.actual_mean_delay > 15 ? 'delay' : (row.actual_mean_delay < 0 ? 'early' : 'on-time');
      return `<tr><td><strong>${esc(row.airport)}</strong></td><td>${historicalTime(row.window_start)}—${historicalTime(row.window_end).slice(11)}</td><td>${fmt(row.flight_count,0)} 班</td><td class="${className}">${fmt(row.actual_mean_delay)} min</td><td>${fmt(row.predicted_mean_delay)} min</td><td>${fmt(row.lower)} ～ ${fmt(row.upper)}</td><td>${fmt(Math.abs(row.predicted_mean_delay-row.actual_mean_delay))} min</td></tr>`;
    }).join('') : '<tr><td colspan="7">这个时间段没有达到最小航班量的机场小时。</td></tr>';
  } catch (error) {
    if (requestId === historyRequest) {
      $('history-status').textContent = error.message;
      $('history-status').classList.add('error');
    }
    throw error;
  }
}

$('history-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(event.submitter, loadHistory);
});
for (const id of ['airport','start','end']) {
  $(id).addEventListener('change', () => {
    $('history-status').classList.remove('error');
    $('history-status').textContent = '筛选条件已更改，点击“更新回测”查看结果。';
  });
}

$('export').addEventListener('click', event => busy(event.currentTarget, async () => {
  if (!currentQuery) throw new Error('请先完成一次机场小时查询。');
  const response = await fetch('/api/export?' + currentQuery);
  if (response.status === 401) { location.replace('/login'); return; }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || '导出失败，请稍后重试。');
  }
  const blob = await response.blob();
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  const disposition = response.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="?([^";]+)"?/i);
  link.download = match?.[1] || 'PEK-hourly.csv';
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  toast('小时级 CSV 已开始下载。');
}));

$('scenario').addEventListener('change', () => {
  $('scenario-fields').hidden = !$('scenario').checked;
});

$('predict-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    if (!$('forecast-time').value.endsWith(':00')) throw new Error('机场小时必须从整点开始。');
    const scenario = $('scenario').checked;
    const rawDst = $('scenario-dst').value.trim();
    const rawRoti = $('scenario-roti').value.trim();
    if (scenario && (!rawDst || !rawRoti)) throw new Error('手动情景需同时填写 Dst 变化率和 ROTI。');
    const previous = $('prediction-result').innerHTML;
    $('prediction-result').classList.add('result-loading');
    $('prediction-result').innerHTML = '<span class="eyebrow">CALCULATING</span><div class="inline-loading"><span></span>正在计算机场小时预报…</div><p>系统正在匹配窗口时段与预报前可用的历史空间天气参数。</p>';
    try {
      const data = await api('/api/predict', {
        airport:$('forecast-airport').value,
        window_start:`${$('forecast-date').value}T${$('forecast-time').value}:00+08:00`,
        horizon_minutes:60,
        space_mode:scenario ? 'manual' : 'auto',
        dst:scenario ? Number(rawDst) : null,
        roti:scenario ? Number(rawRoti) : null
      });
      const metric = meta.airports.find(item => item.code === data.airport) || {};
      $('prediction-result').innerHTML =
        `<div class="eyebrow">${esc(data.airport)} · ${historicalTime(data.window_start)}—${historicalTime(data.window_end).slice(11)}</div>` +
        '<h2>预计机场一小时平均离港延误</h2>' +
        `<div class="prediction-number">${fmt(data.predicted_mean_delay_minutes)}<small>分钟</small></div>` +
        `<div class="range-panel"><small>名义 80% 机场小时校准区间</small><strong>${fmt(data.lower_minutes)} ～ ${fmt(data.upper_minutes)} <small>分钟</small></strong><small>2019 历史留出审计实测覆盖率：${pct(metric.coverage80)}；低于 80% 表示跨年份覆盖不足。</small></div>` +
        `<div class="prediction-facts"><span>启用：${esc(modelLabel(data.model))}</span><span>历史典型航班量：${fmt(data.expected_flights,0)} 班/小时</span><span>星期×时段基线：${fmt(data.simple_baseline_mean_delay_minutes)} min</span></div>` +
        '<p class="pred-detail">这是整个 PEK 机场窗口的平均值，不适用于任何单个航班或具体航线。负值表示该窗口整体可能平均提前离港。</p>' +
        (data.out_of_period ? '<span class="pill amber">当前年份超出验证期，仅供研究参考</span>' : '') +
        (data.low_support ? '<span class="pill amber">该时段历史航班量或支持窗口较少</span>' : '') +
        (scenario ? `<p class="pred-detail">指定情景下模型 B（时间＋空间天气）：${fmt(data.space_scenario_mean_delay_minutes)} min；模型 A（仅时间）：${fmt(data.baseline_model_mean_delay_minutes)} min。${data.model !== 'space' ? '模型 B 尚未通过全部生产门槛，主结果仍使用模型 A。' : ''}${data.outside_space_training ? ' 参数超出历史主要支持范围，不建议解读外推结果。' : ''}</p>` : '');
      if (data.auto_parameters) {
        const parameters = data.auto_parameters;
        $('prediction-result').insertAdjacentHTML('beforeend',
          `<p class="pred-detail">自动参数：${esc(parameters.reason)}${parameters.usable ? `<br>${esc(parameters.source)} · 特征小时 ${esc(localTime(parameters.feature_hour_utc))}<br>|dDst/dt| = ${fmt(parameters.dst)} nT/h；ROTI = ${fmt(parameters.roti,4)} TECU/min。${data.model !== 'space' ? '当前主结果仍采用非空间天气模型。' : ''}` : ''}</p>`
        );
      }
    } catch (error) {
      $('prediction-result').innerHTML = previous;
      throw error;
    } finally {
      $('prediction-result').classList.remove('result-loading');
    }
  });
});

function showWeather(data) {
  if (!data) {
    $('weather-data').innerHTML = '<div class="empty-state"><h3>暂时没有空间天气记录</h3><p>点击“更新空间天气”重新获取公开数据。</p></div>';
    $('auto-weather-status').textContent = '尚未取得空间天气数据。';
    return;
  }
  const dst = data.dst, kp = data.kp, forecast = data.kp_forecast;
  const good = Boolean(dst && !dst.stale && kp && !kp.stale && forecast && !forecast.stale);
  const stamp = item => item?.latest?.time ? `${esc(localTime(item.latest.time))} · ${item.stale ? '已过期' : '有效'}` : '暂时不可用';
  const points = forecast?.series ?? [];
  $('weather-data').innerHTML =
    `<div class="auto-values"><div><small>Dst · 最新观测</small><strong>${fmt(dst?.latest?.dst,0)} <small>nT</small></strong><p>${stamp(dst)}</p></div>` +
    `<div><small>|dDst/dt| · 自动计算</small><strong>${fmt(dst?.abs_rate)} <small>nT/h</small></strong><p>${dst?.abs_rate === null ? '相邻小时不连续，无法计算' : '相邻连续小时差的绝对值'}</p></div>` +
    `<div><small>Kp · 观测估计</small><strong>${fmt(kp?.latest?.kp,2)}</strong><p>${stamp(kp)}</p></div>` +
    '<div><small>北京站 ROTI</small><strong>未接入</strong><p>不以 Kp、零值或历史均值代替</p></div></div>' +
    `<div class="kp-outlook"><div><span class="pill ${forecast && !forecast.stale ? 'teal' : 'amber'}">Kp 预报 · ${forecast && !forecast.stale ? '未来时段' : '暂不可用 / 已过期'}</span><p>NOAA 预报与观测分开显示；不代替 Dst 或 ROTI。</p></div>` +
    (points.length ? `<div class="kp-points">${points.slice(0,6).map(point => `<div><small>${esc(localTime(point.time))}</small><strong>${fmt(point.kp,2)}</strong></div>`).join('')}</div><p class="subtle">数据覆盖至 ${esc(localTime(points.at(-1).time))}，共 ${points.length} 个未来时点。</p>` : '') +
    `</div><p class="subtle">最近请求：${esc(localTime(data.fetched_at))}。${Object.entries(data.errors ?? {}).map(([key,value]) => esc(key) + ': ' + esc(value)).join('；')}</p>`;
  $('auto-weather-status').textContent = good ? 'Dst、Kp 和 Kp 预报已更新；实时 ROTI 仍待接入。' : '部分数据缺失或过期，请查看下方状态。';
}

async function updateWeather(manual=false) {
  if (weatherPending || !meta) return;
  weatherPending = true;
  $('auto-weather-status').textContent = '正在获取 NOAA 空间天气数据…';
  try {
    const data = await api('/api/weather/refresh', {});
    showWeather(data.data);
    lastWeatherCheck = Date.now();
    if (manual) toast(data.cached ? '显示 5 分钟缓存内的数据。' : '空间天气数据已更新。');
  } catch (error) {
    $('auto-weather-status').textContent = '自动更新失败：' + error.message;
    const cache = await api('/api/weather').catch(() => null);
    if (cache?.data) {
      showWeather(cache.data);
      $('auto-weather-status').textContent = '更新失败，下方为缓存记录，请留意观测时间。';
    }
    if (manual) toast(error.message);
  } finally {
    weatherPending = false;
  }
}

$('weather-refresh').addEventListener('click', event => busy(event.currentTarget, () => updateWeather(true)));
setInterval(() => { if (!document.hidden) updateWeather(); }, 300000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && Date.now() - lastWeatherCheck >= 300000) updateWeather();
});

$('key-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = await api('/api/settings', {key:$('api-key').value});
    $('api-key').value = '';
    $('live-badge').textContent = data.configured ? '密钥已配置 · 待检查覆盖' : '尚未配置密钥';
    toast(data.message);
  });
});

$('fetch-live').addEventListener('click', event => busy(event.currentTarget, async () => {
  $('live-status').classList.remove('error');
  $('live-status').textContent = '正在请求机场级航班数据，请稍候…';
  try {
    const data = await api('/api/live', {airport:$('live-airport').value});
    $('live-time').textContent = '抓取于 ' + localTime(data.fetched_at);
    $('live-status').textContent = `${data.source} · 返回 ${fmt(data.returned,0)} 条去重记录${data.cached ? ' · 使用 5 分钟缓存' : ''}${data.complete ? ' · 当前分页完整' : ' · 分页不完整，仅可视为样本'}`;
    $('live-badge').textContent = data.complete ? '分页完整 · 仍需看实际离港覆盖' : '分页不完整 · 样本均值';
    if (!data.windows.length) {
      $('live-content').className = 'empty-state';
      $('live-content').innerHTML = '<h3>接口暂未返回可聚合的 PEK 离港记录</h3><p>这不表示机场没有航班；可能与套餐、日期或数据覆盖有关。</p>';
      return;
    }
    $('live-content').className = 'table-wrap';
    $('live-content').innerHTML = `<div class="coverage-warning ${data.complete ? '' : 'incomplete'}"><strong>${data.complete ? '分页检查通过' : '当前只拿到部分分页'}</strong><span>${esc(data.coverage_note)}</span></div><table><thead><tr><th>机场小时</th><th>返回计划记录</th><th>已有实际离港</th><th>样本实际平均延误</th><th>机场小时预报</th><th>解释</th></tr></thead><tbody>${data.windows.map(window => {
      const prediction = window.prediction;
      const coverage = window.scheduled_flights ? window.observed_flights / window.scheduled_flights : null;
      return `<tr><td>${historicalTime(window.window_start)}—${historicalTime(window.window_end).slice(11)}</td><td>${fmt(window.scheduled_flights,0)}</td><td>${fmt(window.observed_flights,0)}<br><small>${pct(coverage)}</small></td><td>${finite(window.actual_mean_delay) ? fmt(window.actual_mean_delay) + ' min' : '尚未形成'}</td><td>${prediction ? fmt(prediction.predicted_mean_delay_minutes) + ' min' : '暂不可用'}<br><small>${window.prediction_kind === 'prewindow' ? '窗口前已记录' : window.prediction_kind === 'retrospective' ? '事后回算' : ''}</small></td><td>${data.complete && coverage === 1 ? '可作为完整观测窗口' : '仅作覆盖检查，不宣称完整机场均值'}</td></tr>`;
    }).join('')}</tbody></table>`;
  } catch (error) {
    $('live-status').textContent = error.message;
    $('live-status').classList.add('error');
    throw error;
  }
}));

function showMethods() {
  const selected = modelLabel(meta.selected);
  const airport = meta.airports[0];
  $('model-status-chip').textContent = selected + ' · 生产门槛决定';
  $('overview-project-stats').innerHTML =
    stat('有效离港操作', fmt(meta.valid_departures,0), '班次', '去重、非取消且实际离港时间有效') +
    stat('有效机场小时', fmt(meta.valid_windows,0), '个', '每个建模窗口至少 5 班') +
    stat('历史留出窗口', fmt(meta.test?.n ?? meta.splits?.test?.[2],0), '个', '2019 全年 · 不参与训练') +
    stat('当前模型留出 MAE', fmt(meta.test.mae), '分钟', `${selected} · 机场小时等权`, true);
  $('model-stats').innerHTML =
    stat('有效机场小时', fmt(meta.valid_windows,0), '个', `${fmt(meta.valid_departures,0)} 个有效离港操作`) +
    stat('历史留出 MAE', fmt(meta.test.mae), '分钟', '2019 · 小时窗口等权', true) +
    stat('历史留出 RMSE', fmt(meta.test.rmse), '分钟', `Bias ${fmt(meta.test.bias)} 分钟`) +
    stat('80% 区间实测覆盖', pct(airport.coverage80), '', '按机场小时计算');
  const titles = {train:'模型训练', validation:'历史开发评估', calibration:'区间校准', test:'历史留出审计'};
  $('split-timeline').innerHTML = Object.entries(meta.splits).map(([key,value]) => `<div class="split"><b>${titles[key]}</b><small>${esc(value[0])}<br>至 ${esc(value[1])}</small><small>${fmt(value[2],0)} 个窗口</small></div>`).join('');
  $('model-rows').innerHTML = meta.airports.map(item => `<tr><td><strong>${esc(item.code)}</strong><br><small>${esc(item.name)}</small></td><td>${fmt(item.test_windows,0)}</td><td>${fmt(item.mean_flights_per_window)}</td><td>${fmt(item.baseline_model_mae)} min</td><td>${fmt(item.space_model_mae)} min</td><td>${fmt(item.mae)} min</td><td>${pct(item.coverage80)}</td></tr>`).join('');
  const developmentGain = (meta.validation.baseline.mae - meta.validation.space.mae) / meta.validation.baseline.mae * 100;
  const holdoutGain = (meta.baseline_model_test.mae - meta.space_model_test.mae) / meta.baseline_model_test.mae * 100;
  const holdoutCi = meta.test_bootstrap ?? {};
  $('selection-note').textContent = `2018 Q3 历史开发评估：A ${fmt(meta.validation.baseline.mae,2)} 分钟，B ${fmt(meta.validation.space.mae,2)} 分钟，B 改善 ${fmt(developmentGain,2)}%。2019 历史留出审计：A ${fmt(meta.baseline_model_test.mae,2)} 分钟，B ${fmt(meta.space_model_test.mae,2)} 分钟，B 点估计改善仅 ${fmt(holdoutGain,3)}%，且 B-A 的 95% 区间为 ${signed(holdoutCi.lower,3)}～${signed(holdoutCi.upper,3)} 分钟并跨过 0。加上 ROTI 时区未确认、缺少全新确认段，本次生产选择“${selected}”。`;
  $('limitations').innerHTML = meta.limitations.map(item => '<li>' + esc(item) + '</li>').join('');
}

async function loadWeatherPanel() {
  const cached = await api('/api/weather');
  showWeather(cached.data);
  await updateWeather();
}

async function init() {
  try {
    meta = await api('/api/meta');
    $('clock').textContent = meta.today;
    $('account-link').textContent = meta.user.username + ' · 账号';
    $('admin-link').hidden = meta.user.role !== 'admin';
    const next = meta.next_window_start || `${meta.tomorrow}T10:00:00+08:00`;
    $('forecast-date').value = next.slice(0,10);
    $('forecast-time').value = next.slice(11,16);
    $('live-badge').textContent = meta.live_configured ? '密钥已配置 · 待检查覆盖' : '等待数据源连接';
    if (meta.settings_readonly || meta.user.role !== 'admin') {
      $('key-form').hidden = true;
      $('connection-note').textContent = meta.live_configured
        ? '航班接口已由管理员在服务器端安全配置；页面不会显示或返回密钥。'
        : '航班接口需由管理员在服务器环境变量中配置；历史回测和研究预报不受影响。';
    }
    if (meta.sharing_mode) {
      $('environment-note').innerHTML = '<div class="service-line"><span class="signal-dot"></span><strong>公网服务已连接</strong></div><p>PEK 机场小时模型</p><span>HTTPS 访问 · 账号状态持久保存</span>';
    }
    showMethods();
    const initial = sectionNames[location.hash.slice(1)] ? location.hash.slice(1) : 'overview';
    tab(initial, {updateUrl:false, focus:false});
    const results = await Promise.allSettled([loadHistory(), loadWeatherPanel(), loadSpaceComparison()]);
    for (const result of results) if (result.status === 'rejected') console.warn(result.reason);
  } catch (error) {
    $('global-error').hidden = false;
    $('global-error').textContent = error.message;
  }
}

init();
