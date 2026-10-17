'use strict';

const $ = id => document.getElementById(id);
const names = {SHA:'上海虹桥', PVG:'上海浦东', CAN:'广州白云', SZX:'深圳宝安'};
const sectionNames = {overview:'航线概览', forecast:'延误预报', live:'实时航班', methods:'模型与数据'};
let meta = null;
let currentQuery = '';
let historyRequest = 0;
let weatherPending = false;
let lastWeatherCheck = 0;

const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
const fmt = (value, digits=1) => finite(value)
  ? Number(value).toLocaleString('zh-CN', {minimumFractionDigits:digits, maximumFractionDigits:digits})
  : '—';
const pct = value => finite(value) ? fmt(Number(value) * 100, 1) + '%' : '—';
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
  if (options.updateUrl !== false && location.hash !== '#' + name) {
    history.pushState({tab:name}, '', '#' + name);
  }
  window.scrollTo({top:0, behavior:matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
  if (options.focus !== false) page.focus({preventScroll:true});
}

document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => tab(button.dataset.tab)));
document.querySelectorAll('[data-jump]').forEach(button => button.addEventListener('click', () => tab(button.dataset.jump)));
window.addEventListener('popstate', () => tab(location.hash.slice(1) || 'overview', {updateUrl:false, focus:false}));
$('go-forecast').addEventListener('click', () => {
  $('forecast-route').value = $('route').value;
  tab('forecast');
});
$('logout-button').addEventListener('click', event => busy(event.currentTarget, async () => {
  await api('/api/auth/logout', {});
  location.replace('/login');
}));

function stat(label, value, unit, note, featured=false) {
  return `<div class="stat ${featured ? 'featured' : ''}"><div class="stat-label">${esc(label)}<span aria-hidden="true">↗</span></div><div class="stat-value">${value}<small>${esc(unit)}</small></div><div class="stat-note">${esc(note)}</div></div>`;
}

function chart(container, series, label) {
  const valid = series.flatMap(item => item.points.filter(point => Number.isFinite(point.y)));
  if (!valid.length) {
    container.innerHTML = '<div class="empty-state"><p>该时间段暂无有效航班记录。</p></div>';
    return;
  }
  const width = 760, height = 270, padding = {l:46, r:20, t:25, b:40};
  const allDates = [...new Set(valid.map(point => point.x))].sort();
  const timestamps = allDates.map(date => Date.parse(date));
  const minTime = Math.min(...timestamps), maxTime = Math.max(...timestamps);
  let minY = Math.min(0, ...valid.map(point => point.y));
  let maxY = Math.max(10, ...valid.map(point => point.y));
  const yPadding = Math.max(4, (maxY - minY) * .12);
  minY -= yPadding;
  maxY += yPadding;
  const x = date => minTime === maxTime
    ? (padding.l + width - padding.r) / 2
    : padding.l + ((Date.parse(date) - minTime) / (maxTime - minTime)) * (width - padding.l - padding.r);
  const y = value => height - padding.b - (value - minY) / (maxY - minY) * (height - padding.t - padding.b);
  let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title>`;
  for (let index=0; index<=4; index++) {
    const value = minY + (maxY - minY) * index / 4;
    const rowY = y(value);
    svg += `<line x1="${padding.l}" y1="${rowY}" x2="${width-padding.r}" y2="${rowY}" stroke="#e7eeeb"/><text x="${padding.l-10}" y="${rowY+3}" text-anchor="end" fill="#81928f" font-size="10">${value.toFixed(0)}</text>`;
  }
  const every = Math.max(1, Math.ceil(allDates.length / 6));
  allDates.forEach((date, index) => {
    if (index % every === 0 || index === allDates.length - 1) {
      svg += `<text x="${x(date)}" y="${height-13}" text-anchor="middle" fill="#81928f" font-size="10">${esc(date.slice(5,10))}</text>`;
    }
  });
  for (const item of series) {
    let path = '', last = null;
    for (const point of item.points) {
      if (!Number.isFinite(point.y)) {
        last = null;
        continue;
      }
      const gap = last && Date.parse(point.x) - Date.parse(last) > 86400000 * 1.5;
      path += `${!last || gap ? 'M' : 'L'}${x(point.x).toFixed(1)},${y(point.y).toFixed(1)} `;
      last = point.x;
    }
    svg += `<path d="${path}" fill="none" stroke="${item.color}" stroke-width="2.2" ${item.dash ? 'stroke-dasharray="6 5"' : ''} stroke-linecap="round" stroke-linejoin="round"/>`;
    for (const point of item.points) {
      if (!Number.isFinite(point.y)) continue;
      const accessible = `${item.name}，${point.x}，${fmt(point.y)} 分钟`;
      svg += `<circle tabindex="0" role="img" aria-label="${esc(accessible)}" cx="${x(point.x)}" cy="${y(point.y)}" r="${allDates.length > 70 ? 1.7 : 3.2}" fill="${item.color}" stroke="white" stroke-width="1"><title>${esc(accessible)}</title></circle>`;
    }
  }
  container.innerHTML = svg + '</svg>';
}

function validateHistory() {
  if ($('route').value === $('compare').value) throw new Error('目标航线和对比航线不能相同。');
  if (!$('start').value || !$('end').value) throw new Error('请选择完整的开始和结束日期。');
  if ($('start').value > $('end').value) throw new Error('开始日期不能晚于结束日期。');
}

async function loadHistory() {
  validateHistory();
  const requestId = ++historyRequest;
  const query = new URLSearchParams({
    route:$('route').value,
    compare:$('compare').value,
    start:$('start').value,
    end:$('end').value
  });
  $('history-status').classList.remove('error');
  $('history-status').textContent = '正在查询历史航班…';
  $('export').disabled = true;
  try {
    const data = await api('/api/backtest?' + query);
    if (requestId !== historyRequest) return;
    currentQuery = query.toString();
    $('export').disabled = false;
    const focus = data.summary[0] || {n:0};
    const route = query.get('route');
    const modelName = data.selected_model === 'baseline' ? '基础航班模型' : '空间天气模型';
    $('history-status').textContent = `${data.start} — ${data.end} · 独立测试集 · ${modelName} · 查询完成`;
    $('stats').innerHTML =
      stat('匹配航班', fmt(focus.n, 0), '班', '目标航线 · 当前时段') +
      stat('实际平均到港延误', fmt(focus.actual_mean), '分钟', '负值表示提前到达', true) +
      stat('预测平均绝对误差', fmt(focus.mae), '分钟', '单航班 MAE · 越低越好') +
      stat('到港延误 > 15 分钟', focus.n ? pct(focus.delay15) : '—', '', '目标航线 · 实际发生比例');
    $('dest-code').textContent = route;
    $('dest-name').textContent = names[route];
    $('route-comparison').innerHTML = data.summary.map(item =>
      `<div class="route-compare-row"><div class="route-label">PEK → ${esc(item.route)}<small>${fmt(item.n,0)} 班 · 实际平均 DT</small></div><strong>${fmt(item.actual_mean)}<em>min</em></strong></div>`
    ).join('');
    const first = data.series.find(item => item.route === route);
    const second = data.series.find(item => item.route === query.get('compare'));
    const lines = [];
    if (first) {
      lines.push({name:'目标实际', color:'#2b8275', points:first.points.map(point => ({x:point.date, y:point.actual}))});
      lines.push({name:'模型预测', color:'#d28a48', dash:true, points:first.points.map(point => ({x:point.date, y:point.predicted}))});
    }
    if (second && second !== first) {
      lines.push({name:'对比实际', color:'#9aa8bb', points:second.points.map(point => ({x:point.date, y:point.actual}))});
    }
    chart($('history-chart'), lines, '每日平均到港延误：目标实际、模型预测和对比航线实际');
    $('chart-description').textContent = `PEK → ${route} 对比 ${query.get('compare')} · 日平均 DT（分钟）`;
    $('table-caption').textContent = `当前目标航线 ${fmt(data.record_total,0)} 班，页面显示前 ${Math.min(data.record_total,data.record_limit)} 班；下载包含全部筛选记录。`;
    $('history-rows').innerHTML = data.records.length ? data.records.map(flight => {
      const className = flight.arrival_DT > 15 ? 'delay' : (flight.arrival_DT < 0 ? 'early' : 'on-time');
      return `<tr><td>${esc(flight.flight_no)}</td><td>${historicalTime(flight.scheduled)}</td><td class="${className}">${fmt(flight.arrival_DT)} min</td><td>${fmt(flight.predicted_DT)} min</td><td>${fmt(flight.lower)} ～ ${fmt(flight.upper)}</td><td>${fmt(Math.abs(flight.predicted_DT-flight.arrival_DT))} min</td></tr>`;
    }).join('') : '<tr><td colspan="6">这个时间段没有有效航班记录。</td></tr>';
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
for (const id of ['route','compare','start','end']) {
  $(id).addEventListener('change', () => {
    $('history-status').classList.remove('error');
    $('history-status').textContent = '筛选条件已更改，点击“更新对比”查看结果。';
  });
}

$('export').addEventListener('click', event => busy(event.currentTarget, async () => {
  if (!currentQuery) throw new Error('请先完成一次航线查询。');
  const response = await fetch('/api/export?' + currentQuery);
  if (response.status === 401) {
    location.replace('/login');
    return;
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || '导出失败，请稍后重试。');
  }
  const blob = await response.blob();
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  const disposition = response.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="?([^";]+)"?/i);
  link.download = match?.[1] || 'hangji-export.csv';
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  toast('CSV 已开始下载。');
}));

$('scenario').addEventListener('change', () => {
  $('scenario-fields').hidden = !$('scenario').checked;
});

$('predict-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const previous = $('prediction-result').innerHTML;
    $('prediction-result').classList.add('result-loading');
    $('prediction-result').innerHTML = '<span class="eyebrow">CALCULATING</span><div class="inline-loading"><span></span>正在计算延误估计…</div><p>系统正在匹配航线、时刻与可用的历史空间天气参数。</p>';
    try {
      const scenario = $('scenario').checked;
      const data = await api('/api/predict', {
        route:$('forecast-route').value,
        date:$('forecast-date').value,
        time:$('forecast-time').value,
        airline:$('airline').value,
        space_mode:scenario ? 'manual' : 'auto',
        dst:scenario ? Number($('scenario-dst').value) : null,
        roti:scenario ? Number($('scenario-roti').value) : null
      });
      const metric = meta.routes.find(item => item.code === data.route) || {};
      $('prediction-result').innerHTML =
        `<div class="eyebrow">PEK → ${esc(data.route)} · ${esc(names[data.route])}</div>` +
        '<h2>预计到港时间偏差</h2>' +
        `<div class="prediction-number">${fmt(data.predicted_DT)}<small>分钟</small></div>` +
        `<div class="range-panel"><small>名义 80% 单航班预测区间 · 非确定到达时刻</small><strong>${fmt(data.lower)} ～ ${fmt(data.upper)} <small>分钟</small></strong><small>该航线 2019 年实测覆盖率：${pct(metric.coverage80)}；低于 80% 说明区间偏窄。</small></div>` +
        `<p class="pred-detail">计划出发 ${historicalTime(data.scheduled)} · ${data.model === 'baseline' ? '基础航班模型' : '空间天气模型'}<br>负值表示可能提前到达；未包含地面天气、前序航班和当日运行情况。</p>` +
        (data.out_of_period ? '<span class="pill amber">当前年份超出验证期，仅供研究参考</span>' : '') +
        (scenario ? `<p class="pred-detail">指定情景下空间天气模型：${fmt(data.scenario_DT)} min；基础模型：${fmt(data.baseline_DT)} min。${data.model === 'baseline' ? '空间天气模型未达到验证集提升门槛，主结果保留基础模型。' : ''}${data.outside_space_training ? ' 参数超出历史主要支持范围，不建议解读外推结果。' : ''}</p>` : '');
      if (data.auto_parameters) {
        const parameters = data.auto_parameters;
        $('prediction-result').insertAdjacentHTML('beforeend',
          `<p class="pred-detail">自动参数：${esc(parameters.reason)}${parameters.usable ? `<br>${esc(parameters.source)} · ${esc(localTime(parameters.feature_hour_utc))}（北京时间）<br>|dDst/dt| = ${fmt(parameters.dst)} nT/h；ROTI = ${fmt(parameters.roti,4)} TECU/min。${data.model === 'baseline' ? '当前主结果仍采用基础模型。' : ''}` : ''}</p>`
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
    $('weather-status-chip').textContent = '等待更新';
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
    `<div class="kp-outlook"><div><span class="pill ${forecast && !forecast.stale ? 'teal' : 'amber'}">Kp 预报 · ${forecast && !forecast.stale ? '未来时段' : '暂不可用 / 已过期'}</span><p>NOAA 预报，与观测分开显示；不代替 Dst 或 ROTI。</p></div>` +
    (points.length ? `<div class="kp-points">${points.slice(0,6).map(point => `<div><small>${esc(localTime(point.time))}</small><strong>${fmt(point.kp,2)}</strong></div>`).join('')}</div><p class="subtle">数据覆盖至 ${esc(localTime(points.at(-1).time))}（北京时间），共 ${points.length} 个未来时点。</p>` : '') +
    `</div><p class="subtle">最近请求：${esc(localTime(data.fetched_at))}（北京时间）。${Object.entries(data.errors ?? {}).map(([key,value]) => esc(key) + ': ' + esc(value)).join('；')}</p>`;
  $('auto-weather-status').textContent = good ? 'Dst、Kp 和 Kp 预报已更新；实时 ROTI 仍待接入。' : '部分数据缺失或过期，请查看下方状态。';
  $('weather-status-chip').textContent = good ? '公开观测已更新' : '部分数据不可用';
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
setInterval(() => {
  if (!document.hidden) updateWeather();
}, 300000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && Date.now() - lastWeatherCheck >= 300000) updateWeather();
});

$('key-form').addEventListener('submit', event => {
  event.preventDefault();
  busy(event.submitter, async () => {
    const data = await api('/api/settings', {key:$('api-key').value});
    $('api-key').value = '';
    $('live-badge').textContent = data.configured ? '密钥已配置 · 待抓取验证' : '尚未配置密钥';
    toast(data.message);
  });
});

const statuses = {scheduled:'计划', active:'飞行中', landed:'已到达', cancelled:'取消', incident:'异常', diverted:'备降'};
$('fetch-live').addEventListener('click', event => busy(event.currentTarget, async () => {
  $('live-status').classList.remove('error');
  $('live-status').textContent = '正在请求航班服务商，请稍候…';
  try {
    const data = await api('/api/live', {route:$('live-route').value});
    $('live-time').textContent = '抓取于 ' + localTime(data.fetched_at);
    $('live-status').textContent = `${data.source} · 返回 ${data.returned} 条${data.cached ? ' · 使用 5 分钟缓存' : ''}${data.truncated ? ' · 仅首批结果' : ''}`;
    $('live-badge').textContent = '数据源已连接';
    if (!data.flights.length) {
      $('live-content').className = 'empty-state';
      $('live-content').innerHTML = '<h3>接口暂未返回该航线航班</h3><p>这不表示航线没有执飞；可能与数据覆盖、日期或套餐限制有关。</p>';
      return;
    }
    $('live-content').className = 'table-wrap';
    $('live-content').innerHTML = `<table><thead><tr><th>航班 / 状态</th><th>计划出发（北京时间）</th><th>实际到达（北京时间）</th><th>模型估计 DT / 类型</th><th>实际 DT</th><th>偏差</th></tr></thead><tbody>${data.flights.map(flight => `<tr><td>${esc(flight.flight_no)}<br><small>${esc(statuses[flight.status] || flight.status)}</small></td><td>${esc(localTime(flight.scheduled_departure))}</td><td>${esc(localTime(flight.actual_arrival))}</td><td>${flight.prediction ? fmt(flight.prediction.predicted_DT) + ' min' : '不估计'}<br><small>${flight.prediction_kind === 'predeparture' ? '起飞前已记录' : flight.prediction_kind === 'retrospective' ? '事后回算，非事先预报' : ''}</small></td><td>${flight.actual_DT !== null ? fmt(flight.actual_DT) + ' min' : '尚无有效到达记录'}</td><td>${flight.actual_DT !== null && flight.prediction ? fmt(Math.abs(flight.actual_DT-flight.prediction.predicted_DT)) + ' min' : '—'}</td></tr>`).join('')}</tbody></table>`;
  } catch (error) {
    $('live-status').textContent = error.message;
    $('live-status').classList.add('error');
    throw error;
  }
}));

function showMethods() {
  const selected = meta.selected === 'baseline' ? '基础模型' : '空间天气模型';
  $('model-status-chip').textContent = selected + ' · 已验证';
  $('model-stats').innerHTML =
    stat('有效历史航班', fmt(meta.valid_rows,0), '班', '2017—2019 · 四条航线') +
    stat('独立测试 MAE', fmt(meta.test.mae), '分钟', '2019 年 · 未参与训练', true) +
    stat('简单航线中位数基准', fmt(meta.constant_baseline_test.mae), '分钟', meta.test.mae > meta.constant_baseline_test.mae ? '当前模型未优于简单基准' : '当前模型优于简单基准') +
    stat('当前默认模型', selected, '', '按 2018 年验证集选择');
  const titles = {train:'模型训练', validation:'模型选择', calibration:'区间校准', test:'独立测试'};
  $('split-timeline').innerHTML = Object.entries(meta.splits).map(([key,value]) => `<div class="split"><b>${titles[key]}</b><small>${esc(value[0])}<br>至 ${esc(value[1])}</small><small>${fmt(value[2],0)} 班</small></div>`).join('');
  $('model-rows').innerHTML = meta.routes.map(route => `<tr><td>PEK → ${esc(route.code)}</td><td>${fmt(route.n,0)}</td><td>${fmt(route.baseline_mae)} min</td><td>${fmt(route.space_mae)} min</td><td>${fmt(route.mae)} min</td><td>${pct(route.coverage80)}</td></tr>`).join('');
  $('selection-note').textContent = `验证集 MAE：基础模型 ${fmt(meta.validation.baseline.mae,2)} 分钟，空间天气模型 ${fmt(meta.validation.space.mae,2)} 分钟。预设规则为至少改善 1% 才启用空间天气模型，本次选择${selected}。相关曲线上升不等于空间天气特征可以提高预测精度。`;
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
    $('forecast-date').value = meta.tomorrow;
    $('live-badge').textContent = meta.live_configured ? '密钥已配置 · 待抓取验证' : '等待数据源连接';
    if (meta.settings_readonly || meta.user.role !== 'admin') {
      $('key-form').hidden = true;
      $('connection-note').textContent = meta.live_configured
        ? '航班接口已由管理员在服务器端安全配置。'
        : '航班接口需由管理员在服务器环境变量中配置；历史回测和延误估计不受影响。';
    }
    if (meta.sharing_mode) {
      $('environment-note').innerHTML = '<div class="service-line"><span class="signal-dot"></span><strong>公网服务已连接</strong></div><p>账号与会话持久保存</p><span>HTTPS 访问 · 独立年份回测</span>';
    }
    showMethods();
    const initial = sectionNames[location.hash.slice(1)] ? location.hash.slice(1) : 'overview';
    tab(initial, {updateUrl:false, focus:false});
    const results = await Promise.allSettled([loadHistory(), loadWeatherPanel()]);
    for (const result of results) {
      if (result.status === 'rejected') console.warn(result.reason);
    }
  } catch (error) {
    $('global-error').hidden = false;
    $('global-error').textContent = error.message;
  }
}

init();
