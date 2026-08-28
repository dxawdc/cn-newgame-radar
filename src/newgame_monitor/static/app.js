const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const parseDate = (value) => new Date(`${value}T12:00:00`);
const dateKey = (date = new Date()) => {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
};
const addDays = (value, amount) => {
  const date = typeof value === 'string' ? parseDate(value) : new Date(value);
  date.setDate(date.getDate() + amount);
  return date;
};
const startOfWeek = (value) => {
  const date = typeof value === 'string' ? parseDate(value) : new Date(value);
  date.setDate(date.getDate() - date.getDay());
  return date;
};
const todayKey = () => dateKey(new Date());
const initialQuery = new URLSearchParams(location.search);

const state = {
  period: 'day', anchor: todayKey(), page: 1, pageSize: 24,
  sources: new Set(), events: new Set(), category: '', developer: '', q: '',
  dateFrom: '', dateTo: '', sort: 'event_desc', view: 'grid', total: 0, filters: null,
  followed: false, currentDetailId: null,
  dimension: initialQuery.get('view') === 'channel' ? 'channel' : 'product',
  auth: {user: null, csrfToken: '', favoriteCount: 0, permissions: {manage_users:false, manage_admins:false}},
  calendarStart: dateKey(startOfWeek(addDays(new Date(), -7))),
  galleryResizeObserver: null,
};

const lightboxState = {items: [], index: 0, lastFocus: null};

const escapeHTML = (value = '') => String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const sourceTag = (source) => `<span class="source-tag ${source.note ? 'source-incomplete' : ''}">${escapeHTML(source.label)}${source.note ? `<small>${escapeHTML(source.note)}</small>` : ''}</span>`;
const fmtDate = (value) => {
  if (!value) return '日期待定';
  const d = parseDate(value);
  return `${d.getMonth()+1}月${d.getDate()}日`;
};
const fmtFullDate = (value) => {
  if (!value) return '日期待定';
  const d = parseDate(value);
  return `${d.getFullYear()}年${d.getMonth()+1}月${d.getDate()}日`;
};
const fmtTime = (value) => value ? new Intl.DateTimeFormat('zh-CN', {month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(new Date(value)) : '暂无成功批次';
const fmtFollowTime = (value) => value ? new Intl.DateTimeFormat('zh-CN', {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value)) : '';
const appBase = new URL('./', location.href);

function eventTypeParams() {
  if (!state.filters) return [];
  return state.events.size ? [...state.events] : ['__none__'];
}

function defaultEventTypes() {
  return new Set((state.filters?.event_types || []).filter(item => item.default_included !== false).map(item => item.key));
}

async function api(path, params = {}, options = {}) {
  const url = new URL(String(path).replace(/^\/+/, ''), appBase);
  Object.entries(params).forEach(([key, value]) => {
    if (Array.isArray(value)) value.forEach(item => url.searchParams.append(key, item));
    else if (value !== '' && value != null) url.searchParams.set(key, value);
  });
  const headers = {...(options.headers || {})};
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  if (options.csrf && state.auth.csrfToken) headers['X-CSRF-Token'] = state.auth.csrfToken;
  const response = await fetch(url, {...options, headers});
  const type = response.headers.get('content-type') || '';
  const payload = type.includes('application/json') ? await response.json() : null;
  if (!response.ok) throw new Error(payload?.detail || `${response.status} ${response.statusText}`);
  return payload;
}

async function loadSummary() {
  const data = await api('/api/summary', {anchor: state.anchor, view: state.dimension, event_type: eventTypeParams()});
  $('#metricToday').textContent = data.today;
  $('#metricWeek').textContent = data.week;
  $('#metricMonth').textContent = data.month;
  $('#metricAll').textContent = data.all;
  $('#metricSources').textContent = `${data.sources} 个渠道`;
  $('#footerCount').textContent = `${data.events} EVENTS INDEXED`;
  $('#lastUpdated').textContent = `最近采集 ${fmtTime(data.last_success_at)}`;
  $('#healthDot').classList.toggle('live', Boolean(data.last_success_at));
  $('#issueNo').textContent = state.anchor.replaceAll('-', '');
}

async function loadCalendar() {
  const data = await api('/api/calendar', {
    start: state.calendarStart, days: 28, view: state.dimension,
    source: [...state.sources], event_type: eventTypeParams(),
    category: state.category, developer: state.developer, q: state.q,
  });
  const weekdays = ['日','一','二','三','四','五','六'];
  const firstDay = data.days[0]?.date || state.calendarStart;
  const lastDay = data.days.at(-1)?.date || dateKey(addDays(state.calendarStart, 27));
  $('#calendarWindow').textContent = `${fmtDate(firstDay)} — ${fmtDate(lastDay)}`;
  $('#calendarRail').innerHTML = data.days.map(day => {
    const d = parseDate(day.date);
    const isToday = day.date === todayKey();
    const rangeStart = day.date === state.dateFrom;
    const rangeEnd = day.date === state.dateTo;
    const inRange = Boolean(state.dateFrom && state.dateTo && day.date > state.dateFrom && day.date < state.dateTo);
    const isPeriodDay = !state.dateFrom && !state.dateTo && state.period === 'day' && day.date === state.anchor;
    const classes = [day.count ? 'has-events' : '', isToday ? 'today' : '', rangeStart ? 'range-start' : '', rangeEnd ? 'range-end' : '', inRange ? 'in-range' : '', isPeriodDay ? 'active' : ''].filter(Boolean).join(' ');
    const countUnit = state.dimension === 'product' ? '个产品事件' : '条渠道事件';
    return `<button class="calendar-day ${classes}" data-date="${day.date}" aria-label="${day.date}，${day.count || 0} ${countUnit}" aria-pressed="${rangeStart || rangeEnd || inRange || isPeriodDay}">
      <span class="weekday">周${weekdays[d.getDay()]}</span><span class="day-number">${String(d.getDate()).padStart(2,'0')}</span>
      <span class="event-num"><strong>${day.count || 0}</strong><em> ${countUnit}</em></span></button>`;
  }).join('');
  $$('.calendar-day').forEach(button => button.addEventListener('click', () => selectCalendarDate(button.dataset.date)));
}

async function loadFilters() {
  state.filters = await api('/api/filters');
  state.events = defaultEventTypes();
  $('#eventFilters').innerHTML = state.filters.event_types.map(item => `<label class="source-check event-check ${item.key === 'first_seen' ? 'is-discovery' : ''}"><input type="checkbox" value="${item.key}" ${state.events.has(item.key) ? 'checked' : ''}><i></i><span>${escapeHTML(item.label)}</span>${item.note ? `<small class="event-check-note">${escapeHTML(item.note)}</small>` : ''}</label>`).join('');
  $('#sourceFilters').innerHTML = state.filters.sources.map(item => `<label class="source-check"><input type="checkbox" value="${item.key}"><i></i><span>${escapeHTML(item.label)}</span>${item.note ? `<small class="source-check-note">${escapeHTML(item.note)}</small>` : ''}</label>`).join('');
  $('#categorySelect').insertAdjacentHTML('beforeend', state.filters.categories.map(x => `<option>${escapeHTML(x)}</option>`).join(''));
  $('#developerSelect').insertAdjacentHTML('beforeend', state.filters.developers.map(x => `<option>${escapeHTML(x)}</option>`).join(''));
  $$('.event-filter-list .event-check input').forEach(input => input.addEventListener('change', () => {
    input.checked ? state.events.add(input.value) : state.events.delete(input.value);
    state.page = 1; loadSummary(); loadCalendar(); loadGames(); updateFilterPills();
  }));
  $$('.source-filter-list:not(.event-filter-list) .source-check input').forEach(input => input.addEventListener('change', () => {
    input.checked ? state.sources.add(input.value) : state.sources.delete(input.value);
    state.page = 1; loadCalendar(); loadGames(); updateFilterPills();
  }));
}

function gameCard(game, index) {
  const event = game.featured_event || {};
  const icon = game.icon_url
    ? `<img class="game-icon" src="${escapeHTML(game.icon_url)}" alt="${escapeHTML(game.name)} 图标" loading="lazy" onerror="this.outerHTML='<span class=&quot;icon-fallback&quot;>${escapeHTML(game.name.slice(0,1))}</span>'">`
    : `<span class="icon-fallback">${escapeHTML(game.name.slice(0,1))}</span>`;
  const eventSources = game.event_sources || game.sources || [];
  const sources = eventSources.slice(0, 3).map(sourceTag).join('');
  const more = eventSources.length > 3 ? `<span class="source-tag source-more">+${eventSources.length-3}</span>` : '';
  const tags = [game.category, event.type_label, ...(game.tags || [])].filter(Boolean).filter((x,i,a)=>a.indexOf(x)===i).slice(0,4);
  const dateNote = state.dimension === 'product'
    ? (event.date_precision === 'discovered' ? '最早首次采集发现' : `最早渠道${event.type_label || '事件'}`)
    : (event.date_precision === 'discovered' ? '首次采集发现' : (event.type_label || '渠道事件'));
  const sourceContext = state.dimension === 'product' ? '最早发生渠道' : '事件来源';
  const laterEvents = state.dimension === 'product' && game.later_event_count
    ? `<p class="later-events">后续还有 <strong>${game.later_event_count}</strong> 个同类渠道事件，完整轨迹见详情</p>` : '';
  const followLabel = game.followed ? '已关注' : '＋ 关注';
  return `<article class="game-card" data-id="${game.id}" data-uuid="${escapeHTML(game.uuid)}" data-index="${String(index+1).padStart(2,'0')}">
    <button class="card-follow ${game.followed ? 'active' : ''}" type="button" data-follow-key="${escapeHTML(game.uuid)}" aria-pressed="${Boolean(game.followed)}">${followLabel}</button>
    <div class="card-head">${icon}<div><div class="event-date"><i></i>${fmtDate(event.date)} · ${escapeHTML(dateNote)}</div>
    <h3>${escapeHTML(game.name)}</h3><p class="developer">${escapeHTML(game.developer || '开发商待补充')}</p></div></div>
    <p class="game-intro">${escapeHTML(game.intro || '该渠道暂未提供玩法介绍，已保留来源事件等待详情补全。')}</p>
    <div class="game-meta">${tags.map(tag => `<span class="meta-tag">${escapeHTML(tag)}</span>`).join('')}</div>
    <div class="source-row"><span class="source-context">${sourceContext}</span>${sources}${more}</div>
    ${laterEvents}
    <p class="follow-date" ${game.followed && game.last_followed_at ? '' : 'hidden'}>最近关注 <time>${escapeHTML(fmtFollowTime(game.last_followed_at))}</time></p></article>`;
}

async function loadGames() {
  $('#gameGrid').innerHTML = '<div class="loading">正在扫描渠道事件…</div>';
  try {
    const params = {
      period: state.dateFrom || state.dateTo ? 'all' : state.period, anchor: state.anchor,
      date_from: state.dateFrom, date_to: state.dateTo, source: [...state.sources],
      event_type: eventTypeParams(), category: state.category, developer: state.developer,
      q: state.q, sort: state.sort, page: state.page, page_size: state.pageSize,
      followed: state.followed, view: state.dimension,
    };
    const data = await api('/api/games', params); state.total = data.total;
    $('#gameGrid').innerHTML = data.items.map(gameCard).join('');
    $('#gameGrid').classList.toggle('list-view', state.view === 'list');
    $('#emptyState').hidden = data.total !== 0;
    $('#resultMeta').textContent = state.dimension === 'product'
      ? `命中 ${data.total} 个产品事件 · 涉及 ${data.product_total} 款独立产品 · 第 ${data.page} 页`
      : `命中 ${data.total} 条渠道事件 · 涉及 ${data.product_total} 款独立产品 · 第 ${data.page} 页`;
    const pages = Math.max(1, Math.ceil(data.total / state.pageSize));
    $('#pageInfo').textContent = `${state.page} / ${pages}`;
    $('#prevPage').disabled = state.page <= 1; $('#nextPage').disabled = state.page >= pages;
    $$('.game-card').forEach(card => card.addEventListener('click', () => openDetail(card.dataset.uuid)));
    $$('.card-follow').forEach(button => button.addEventListener('click', event => {
      event.stopPropagation(); toggleFavorite(button.dataset.followKey, button);
    }));
  } catch (error) {
    $('#gameGrid').innerHTML = `<div class="empty-state"><span>!</span><h3>数据服务未响应</h3><p>${escapeHTML(error.message)}</p></div>`;
  }
}

async function openDetail(gameUuid) {
  state.galleryResizeObserver?.disconnect();
  state.galleryResizeObserver = null;
  state.currentDetailId = String(gameUuid);
  $('#drawerContent').innerHTML = '<p>正在读取产品档案…</p>';
  $('#detailDrawer').classList.add('open'); $('#detailDrawer').setAttribute('aria-hidden','false'); $('#filterBackdrop').classList.add('open');
  const game = await api(`/api/v2/games/${encodeURIComponent(gameUuid)}`);
  const icon = game.icon_url ? `<img class="game-icon" src="${escapeHTML(game.icon_url)}" alt="${escapeHTML(game.name)} 图标">` : `<span class="icon-fallback">${escapeHTML(game.name.slice(0,1))}</span>`;
  const latestIntro = game.latest_intro || (game.intro ? {text: game.intro, source_label: '聚合资料'} : null);
  const introMeta = latestIntro?.collected_at ? `${escapeHTML(latestIntro.source_label)} · 最近采集 ${fmtTime(latestIntro.collected_at)}` : escapeHTML(latestIntro?.source_label || '');
  const introLink = latestIntro?.detail_url ? `<a href="${escapeHTML(latestIntro.detail_url)}" target="_blank" rel="noreferrer">查看详情来源 ↗</a>` : '';
  const introTitle = latestIntro?.kind === 'full' ? '游戏介绍 / 详情' : '游戏简介';
  const gallery = game.gallery || [];
  const galleryColumns = gallery.length === 1 ? 'is-single' : gallery.length === 2 ? 'is-double' : 'is-multiple';
  const gallerySection = gallery.length ? `<section class="store-gallery"><div class="gallery-heading"><div><span>STORE MEDIA</span><h3>图集</h3></div><small>${gallery.length} 张 · 左右切换查看</small></div>
    <div class="gallery-carousel ${galleryColumns}" aria-label="${escapeHTML(game.name)} 图集">
      <div class="gallery-viewport" tabindex="0">
        <div class="gallery-track">${gallery.map((image,index) => `<a class="gallery-slide" href="${escapeHTML(image.url)}" target="_blank" rel="noreferrer" data-gallery-index="${index}" data-source-label="${escapeHTML(image.source_label)}" aria-label="查看${escapeHTML(game.name)}图集第 ${index + 1} 张大图，来源${escapeHTML(image.source_label)}"><img src="${escapeHTML(image.url)}" alt="${escapeHTML(game.name)} 图集 ${index + 1}" loading="lazy"></a>`).join('')}</div>
      </div>
      <button class="gallery-nav gallery-prev" type="button" aria-label="上一张"><img src="assets/icons/chevron-left.svg" alt=""></button>
      <button class="gallery-nav gallery-next" type="button" aria-label="下一张"><img src="assets/icons/chevron-right.svg" alt=""></button>
      <div class="gallery-progress" aria-live="polite"><span>图集</span><strong data-gallery-current>1</strong><span>/${gallery.length}</span></div>
    </div></section>` : '';
  $('#drawerContent').innerHTML = `<div class="drawer-hero">${icon}<div><p class="eyebrow">PRODUCT DOSSIER / ${game.source_count} SOURCES</p><h2>${escapeHTML(game.name)}</h2><p>${escapeHTML(game.developer || '开发商待补充')} · ${escapeHTML(game.category || '品类待补充')}</p><button class="drawer-follow ${game.followed ? 'active' : ''}" type="button" data-follow-key="${escapeHTML(game.uuid)}" aria-pressed="${Boolean(game.followed)}">${game.followed ? '已关注此游戏' : '＋ 添加关注'}</button></div></div>
    <p class="detail-follow-date" ${game.followed && game.last_followed_at ? '' : 'hidden'}>最近一次关注：<time>${escapeHTML(fmtFollowTime(game.last_followed_at))}</time></p>
    ${gallerySection}
    <section class="latest-intro ${latestIntro?.kind === 'full' ? 'is-full' : ''}"><div class="latest-intro-head"><div><span>GAME PROFILE</span><h3>${introTitle}</h3></div>${introLink}</div>
      <p>${escapeHTML(latestIntro?.text || '当前渠道暂未提供游戏介绍，后续采集到详情后会自动补充。')}</p>
      ${introMeta ? `<small><i></i>${introMeta}</small>` : ''}</section>
    <div class="source-row">${game.sources.map(sourceTag).join('')}</div>
    <section class="drawer-section"><h3>渠道事件轨迹</h3><div class="event-timeline">${game.events.map(event => { const note = event.date_precision === 'discovered' ? `首次采集发现${event.status ? ` · ${event.status}` : ''}` : (event.status || ''); const sourceLabel = event.source_note ? `${event.source_label}（${event.source_note}）` : event.source_label; return `<div class="event-item"><time>${fmtFullDate(event.date)}${event.end_date ? ` — ${fmtFullDate(event.end_date)}` : ''}</time><strong>${escapeHTML(event.type_label)} · ${escapeHTML(sourceLabel)}</strong><p>${escapeHTML(note)}</p>${event.detail_url ? `<a href="${escapeHTML(event.detail_url)}" target="_blank" rel="noreferrer">查看来源 ↗</a>` : ''}</div>`; }).join('')}</div></section>
    <section class="drawer-section"><h3>玩法标签</h3><div class="chip-list">${(game.tags || []).map(tag => `<span class="filter-chip">${escapeHTML(tag)}</span>`).join('') || '暂无标签'}</div></section>`;
  $('.drawer-follow').addEventListener('click', event => toggleFavorite(event.currentTarget.dataset.followKey, event.currentTarget));
  setupGalleryCarousel();
}

function setupGalleryCarousel() {
  const carousel = $('.gallery-carousel');
  if (!carousel) return;
  const viewport = carousel.querySelector('.gallery-viewport');
  const slides = [...carousel.querySelectorAll('.gallery-slide')];
  const previous = carousel.querySelector('.gallery-prev');
  const next = carousel.querySelector('.gallery-next');
  const current = carousel.querySelector('[data-gallery-current]');
  let frame = null;

  const step = () => {
    if (slides.length < 2) return viewport.clientWidth;
    return slides[1].offsetLeft - slides[0].offsetLeft;
  };
  const activeIndex = () => {
    const firstVisible = Math.round(viewport.scrollLeft / Math.max(1, step()));
    const visibleCount = Math.max(1, Math.round(viewport.clientWidth / Math.max(1, step())));
    return Math.max(0, Math.min(slides.length - 1, firstVisible + Math.floor(visibleCount / 2)));
  };
  const update = () => {
    const index = activeIndex();
    current.textContent = String(index + 1);
    previous.classList.toggle('is-hidden', index === 0);
    const endReached = viewport.scrollLeft + viewport.clientWidth >= viewport.scrollWidth - 2;
    next.classList.toggle('is-hidden', endReached || slides.length < 2);
    frame = null;
  };
  const move = direction => viewport.scrollBy({left: direction * step(), behavior: 'smooth'});

  slides.forEach((slide, index) => slide.addEventListener('click', event => {
    event.preventDefault();
    openGalleryLightbox(slides, index);
  }));
  previous.addEventListener('click', () => move(-1));
  next.addEventListener('click', () => move(1));
  viewport.addEventListener('scroll', () => {
    if (!frame) frame = requestAnimationFrame(update);
  }, {passive: true});
  viewport.addEventListener('keydown', event => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    move(event.key === 'ArrowLeft' ? -1 : 1);
  });

  const readImageRatio = image => new Promise(resolve => {
    if (image.complete) {
      resolve(image.naturalWidth && image.naturalHeight ? image.naturalWidth / image.naturalHeight : null);
      return;
    }
    let settled = false;
    const finish = ratio => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(ratio);
    };
    const timer = setTimeout(() => finish(null), 3500);
    image.addEventListener('load', () => finish(image.naturalWidth / image.naturalHeight), {once: true});
    image.addEventListener('error', () => finish(null), {once: true});
  });

  Promise.all(slides.map(slide => readImageRatio(slide.querySelector('img')))).then(values => {
    if (!carousel.isConnected) return;
    const ratios = values.filter(value => Number.isFinite(value) && value > 0).sort((a, b) => a - b);
    if (!ratios.length) return;
    const middle = Math.floor(ratios.length / 2);
    const medianRatio = ratios.length % 2 ? ratios[middle] : (ratios[middle - 1] + ratios[middle]) / 2;
    const orientation = medianRatio >= 1.2 ? 'landscape' : medianRatio <= .82 ? 'portrait' : 'square';
    const limits = {
      landscape: [160, 260],
      square: [220, 360],
      portrait: [300, 500],
    }[orientation];

    carousel.classList.remove('is-landscape', 'is-square', 'is-portrait');
    carousel.classList.add(`is-${orientation}`);
    carousel.dataset.galleryOrientation = orientation;

    const resizeGallery = () => requestAnimationFrame(() => {
      if (!carousel.isConnected) return;
      const slideWidth = slides[0]?.getBoundingClientRect().width || viewport.clientWidth;
      const height = Math.max(limits[0], Math.min(limits[1], slideWidth / medianRatio));
      carousel.style.setProperty('--gallery-card-height', `${Math.round(height)}px`);
      update();
    });

    resizeGallery();
    if ('ResizeObserver' in window) {
      state.galleryResizeObserver?.disconnect();
      state.galleryResizeObserver = new ResizeObserver(resizeGallery);
      state.galleryResizeObserver.observe(viewport);
    } else {
      window.addEventListener('resize', resizeGallery, {passive: true, once: true});
    }
  });
  update();
}

function renderLightbox() {
  const item = lightboxState.items[lightboxState.index];
  if (!item) return;
  $('#lightboxImage').src = item.url;
  $('#lightboxImage').alt = item.alt;
  $('#lightboxTitle').textContent = item.title;
  $('#lightboxSource').textContent = item.source;
  $('#lightboxCurrent').textContent = String(lightboxState.index + 1);
  $('#lightboxTotal').textContent = String(lightboxState.items.length);
  $('#lightboxOriginal').href = item.url;
  const single = lightboxState.items.length < 2;
  $('#lightboxPrev').hidden = single;
  $('#lightboxNext').hidden = single;
}

function openGalleryLightbox(slides, index) {
  lightboxState.items = slides.map(slide => {
    const image = slide.querySelector('img');
    return {
      url: slide.href,
      alt: image.alt,
      title: image.alt.replace(/\s+图集\s+\d+$/, ''),
      source: slide.dataset.sourceLabel || '聚合图集',
    };
  });
  lightboxState.index = index;
  lightboxState.lastFocus = document.activeElement;
  renderLightbox();
  $('#imageLightbox').hidden = false;
  document.body.classList.add('lightbox-open');
  $('#lightboxClose').focus();
}

function moveLightbox(direction) {
  const total = lightboxState.items.length;
  if (total < 2) return;
  lightboxState.index = (lightboxState.index + direction + total) % total;
  renderLightbox();
}

function closeLightbox() {
  const lightbox = $('#imageLightbox');
  if (lightbox.hidden) return;
  lightbox.hidden = true;
  document.body.classList.remove('lightbox-open');
  lightboxState.lastFocus?.focus();
}

function closeOverlays() {
  $('#detailDrawer').classList.remove('open'); $('#detailDrawer').setAttribute('aria-hidden','true');
  $('#accountDrawer').classList.remove('open'); $('#accountDrawer').setAttribute('aria-hidden','true');
  $('#filtersPanel').classList.remove('open'); $('#filterBackdrop').classList.remove('open');
  state.galleryResizeObserver?.disconnect();
  state.galleryResizeObserver = null;
  state.currentDetailId = null;
}

const roleLabels = {superadmin:'超级管理员', admin:'管理员', user:'普通用户'};

function setFormMessage(selector, message = '', type = '') {
  const node = $(selector);
  node.textContent = message;
  node.className = `form-message ${type}`.trim();
}

function openAccountDrawer() {
  $('#accountDrawer').classList.add('open');
  $('#accountDrawer').setAttribute('aria-hidden','false');
  $('#filterBackdrop').classList.add('open');
  setTimeout(() => (state.auth.user ? $('#profileDisplayInput') : $('#loginUsername')).focus(), 260);
}

function renderAuth() {
  const user = state.auth.user;
  $('#loginPanel').hidden = Boolean(user);
  $('#accountCenter').hidden = !user;
  $('#followFilterBlock').hidden = !user;
  $('#accountButton').classList.toggle('signed-in', Boolean(user));
  $('#favoriteCount').textContent = state.auth.favoriteCount;
  if (!user) {
    $('#accountAvatar').textContent = '访';
    $('#accountLabel').textContent = '账号登录';
    $('#accountRole').textContent = '保存关注与导出列表';
    state.followed = false;
    $('#followedOnly').classList.remove('active');
    $('#generatedApiKey').hidden = true;
    $('#generatedApiKeyValue').textContent = '';
    return;
  }
  const first = (user.display_name || user.username).slice(0,1).toUpperCase();
  $('#accountAvatar').textContent = first;
  $('#accountLabel').textContent = user.display_name;
  $('#accountRole').textContent = `${roleLabels[user.role]} · ${state.auth.favoriteCount} 个关注`;
  $('#profileAvatar').textContent = first;
  $('#profileDisplayName').textContent = user.display_name;
  $('#profileUsername').textContent = user.username;
  $('#profileRole').textContent = roleLabels[user.role];
  $('#profileDisplayInput').value = user.display_name;
  $('#usersTabButton').hidden = !state.auth.permissions.manage_users;
  $('#newRoleLabel').hidden = !state.auth.permissions.manage_admins;
  $('#newUserRole').value = 'user';
  $('#userScopeNote').textContent = state.auth.permissions.manage_admins ? '可管理所有角色账号' : '仅显示并管理普通用户';
  $('#apiEndpoint').textContent = new URL('api/v1/favorites', appBase).href;
}

async function loadAuth() {
  try {
    const data = await api('/api/auth/me');
    state.auth = {
      user: data.user, csrfToken: data.csrf_token, favoriteCount: data.favorite_count,
      permissions: data.permissions,
    };
  } catch (_) {
    state.auth = {user:null, csrfToken:'', favoriteCount:0, permissions:{manage_users:false, manage_admins:false}};
  }
  renderAuth();
}

function refreshFollowButtons(gameKey, followed, lastFollowedAt = '') {
  $$('[data-follow-key]').filter(button => button.dataset.followKey === gameKey).forEach(button => {
    button.classList.toggle('active', followed);
    button.setAttribute('aria-pressed', String(followed));
    button.textContent = button.classList.contains('drawer-follow')
      ? (followed ? '已关注此游戏' : '＋ 添加关注')
      : (followed ? '已关注' : '＋ 关注');
  });
  $$('.game-card').filter(card => card.dataset.uuid === gameKey).forEach(card => {
    const note = card.querySelector('.follow-date');
    if (!note) return;
    note.hidden = !followed;
    if (followed) note.querySelector('time').textContent = fmtFollowTime(lastFollowedAt || new Date().toISOString());
  });
  const detailNote = $('.detail-follow-date');
  if (detailNote && $('.drawer-follow')?.dataset.followKey === gameKey) {
    detailNote.hidden = !followed;
    if (followed) detailNote.querySelector('time').textContent = fmtFollowTime(lastFollowedAt || new Date().toISOString());
  }
}

async function toggleFavorite(gameKey, button) {
  if (!state.auth.user) {
    openAccountDrawer();
    setFormMessage('#loginMessage', '登录后即可添加关注。');
    return;
  }
  const followed = button.getAttribute('aria-pressed') === 'true';
  button.disabled = true;
  try {
    const data = followed
      ? await api('/api/favorites', {game_uuid:gameKey}, {method:'DELETE', csrf:true})
      : await api('/api/favorites', {}, {method:'POST', csrf:true, body:{game_uuid:gameKey}});
    state.auth.favoriteCount = data.favorite_count;
    refreshFollowButtons(gameKey, data.followed, data.last_followed_at);
    renderAuth();
    if (state.followed && !data.followed) await loadGames();
  } catch (error) {
    button.textContent = '保存失败';
    setTimeout(() => refreshFollowButtons(gameKey, followed), 1200);
  } finally {
    button.disabled = false;
  }
}

function activateAccountTab(name) {
  $$('.account-tabs button').forEach(button => button.classList.toggle('active', button.dataset.accountTab === name));
  $$('.account-tab').forEach(tab => tab.classList.toggle('active', tab.id === `${name}Tab`));
  if (name === 'users') loadUsers();
  if (name === 'api') loadApiKeys();
}

function renderApiKeys(items) {
  $('#apiKeyList').innerHTML = items.length ? items.map(item => `<article class="api-key-item" data-key-id="${item.id}">
    <div><strong>${escapeHTML(item.name)}</strong><code>${escapeHTML(item.prefix)}••••••••</code><small>创建 ${fmtFollowTime(item.created_at)}${item.last_used_at ? ` · 最近调用 ${fmtFollowTime(item.last_used_at)}` : ' · 尚未调用'}</small></div>
    <button type="button" data-revoke-api-key>撤销</button></article>`).join('') : '<div class="api-key-empty">还没有有效 API Key。创建后可从外部系统读取当前账号的关注列表。</div>';
}

async function loadApiKeys() {
  $('#apiKeyList').innerHTML = '<div class="api-key-empty">正在读取密钥…</div>';
  try {
    const data = await api('/api/account/api-keys');
    renderApiKeys(data.items);
  } catch (error) {
    $('#apiKeyList').innerHTML = `<div class="api-key-empty">${escapeHTML(error.message)}</div>`;
  }
}

async function revokeApiKey(button) {
  const card = button.closest('.api-key-item');
  if (button.dataset.confirm !== 'true') {
    button.dataset.confirm = 'true'; button.textContent = '再次点击确认';
    setTimeout(() => { if (button.isConnected) { button.dataset.confirm = ''; button.textContent = '撤销'; } }, 3000);
    return;
  }
  button.disabled = true;
  try {
    await api(`/api/account/api-keys/${card.dataset.keyId}`, {}, {method:'DELETE', csrf:true});
    await loadApiKeys();
    setFormMessage('#apiKeyMessage', 'API Key 已撤销。', 'success');
  } catch (error) {
    button.disabled = false; setFormMessage('#apiKeyMessage', error.message, 'error');
  }
}

function roleOptions(selected) {
  return Object.entries(roleLabels).map(([value,label]) => `<option value="${value}" ${value === selected ? 'selected' : ''}>${label}</option>`).join('');
}

function renderUsers(items) {
  const actor = state.auth.user;
  $('#userList').innerHTML = items.length ? items.map(user => {
    const manageable = user.id !== actor.id && (actor.role === 'superadmin' || user.role === 'user');
    return `<article class="user-card" data-user-id="${user.id}">
      <div class="user-summary"><div><h4>${escapeHTML(user.display_name)} <span class="role-mark ${user.role}">${roleLabels[user.role]}</span></h4><p>${escapeHTML(user.username)} · ${user.is_active ? '正常使用' : '已停用'}</p></div>
        <div class="user-actions">${manageable ? '<button type="button" data-user-action="edit">管理</button>' : '<small>当前账号</small>'}</div></div>
      ${manageable ? `<form class="user-editor account-form compact-form">
        <div class="form-grid"><label><span>登录账号</span><input data-field="username" value="${escapeHTML(user.username)}" required></label><label><span>显示名</span><input data-field="display_name" value="${escapeHTML(user.display_name)}" required></label></div>
        <div class="form-grid"><label><span>重置密码（留空不修改）</span><input data-field="password" type="password" minlength="8"></label><label><span>角色</span><select data-field="role" ${state.auth.permissions.manage_admins ? '' : 'disabled'}>${roleOptions(user.role)}</select></label></div>
        <label class="status-check"><input data-field="is_active" type="checkbox" ${user.is_active ? 'checked' : ''}> <span>允许该账号登录</span></label>
        <p class="form-message" role="status"></p>
        <div class="user-editor-actions"><button class="account-primary" type="submit">保存修改</button><button class="delete-user" type="button" data-user-action="delete">删除账号</button></div>
      </form>` : ''}</article>`;
  }).join('') : '<div class="user-empty">当前权限范围内还没有其他账号。</div>';
}

async function loadUsers() {
  $('#userList').innerHTML = '<div class="user-empty">正在读取账号列表…</div>';
  try {
    const data = await api('/api/admin/users');
    renderUsers(data.items);
  } catch (error) {
    $('#userList').innerHTML = `<div class="user-empty">${escapeHTML(error.message)}</div>`;
  }
}

async function saveManagedUser(form) {
  const card = form.closest('.user-card');
  const field = name => form.querySelector(`[data-field="${name}"]`);
  const payload = {
    username: field('username').value.trim(), display_name: field('display_name').value.trim(),
    is_active: field('is_active').checked,
  };
  if (field('password').value) payload.password = field('password').value;
  if (state.auth.permissions.manage_admins) payload.role = field('role').value;
  const message = form.querySelector('.form-message');
  try {
    await api(`/api/admin/users/${card.dataset.userId}`, {}, {method:'PATCH', csrf:true, body:payload});
    message.textContent = '账号信息已保存。'; message.className = 'form-message success';
    await loadUsers();
  } catch (error) {
    message.textContent = error.message; message.className = 'form-message error';
  }
}

async function deleteManagedUser(button) {
  const card = button.closest('.user-card');
  if (button.dataset.confirm !== 'yes') {
    button.dataset.confirm = 'yes'; button.textContent = '再次点击确认删除';
    setTimeout(() => { button.dataset.confirm = ''; button.textContent = '删除账号'; }, 3500);
    return;
  }
  button.disabled = true;
  try {
    await api(`/api/admin/users/${card.dataset.userId}`, {}, {method:'DELETE', csrf:true});
    await loadUsers();
  } catch (error) {
    const message = card.querySelector('.form-message');
    message.textContent = error.message; message.className = 'form-message error';
    button.disabled = false;
  }
}

function normalizedRange(from, to) {
  if (from && to && from > to) return [to, from];
  return [from, to];
}

function updateDimensionUI() {
  const productMode = state.dimension === 'product';
  $$('.dimension-switch button').forEach(button => {
    const active = button.dataset.dimension === state.dimension;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  $('#dimensionHint').textContent = productMode
    ? '同一产品的同类事件只保留当前渠道范围内的最早日期；来源筛选会重新计算最早日期。'
    : '每个渠道事件独立展示；同一产品可在不同渠道、不同日期重复出现。';
  $('#heroDimensionNote').textContent = productMode
    ? '同款产品按事件类型聚合，只在当前渠道范围内的最早日期出现；详情仍保留所有来源证据。'
    : '按渠道逐条查看原始事件，同款产品在不同渠道或日期可重复出现。';
  const metricUnit = productMode ? '产品聚合口径' : '渠道事件口径';
  $('#metricTodayUnit').textContent = productMode ? '个产品事件' : '条渠道事件';
  $('#metricWeekUnit').textContent = metricUnit;
  $('#metricMonthUnit').textContent = metricUnit;
  $('#emptyState').querySelector('p').textContent = productMode
    ? '试试放宽日期、渠道或事件类型；产品聚合只落在同类事件的最早日期。'
    : '试试放宽日期、渠道或事件类型。';
}

function setDimension(dimension) {
  if (!['product', 'channel'].includes(dimension) || dimension === state.dimension) return;
  state.dimension = dimension;
  state.page = 1;
  const url = new URL(location.href);
  url.searchParams.set('view', dimension);
  history.replaceState(null, '', url);
  updateDimensionUI();
  loadSummary();
  loadCalendar();
  loadGames();
}

function updateDateWorkbench() {
  const hasRange = Boolean(state.dateFrom || state.dateTo);
  $('#dateFrom').value = state.dateFrom;
  $('#dateTo').value = state.dateTo;
  $('#dateWorkbench').classList.toggle('active', hasRange);
  $('#clearDateRange').hidden = !hasRange;
  if (state.dateFrom && state.dateTo) {
    $('#dateRangeStatus').textContent = state.dateFrom === state.dateTo
      ? `已选择 ${fmtDate(state.dateFrom)}`
      : `已选择 ${fmtDate(state.dateFrom)} — ${fmtDate(state.dateTo)}`;
    $('#calendarSelectionHint').textContent = state.dateFrom === state.dateTo
      ? '再点另一天，可扩展为日期范围'
      : '日期范围已联动到下方新游列表';
  } else if (state.dateFrom || state.dateTo) {
    $('#dateRangeStatus').textContent = `已设置${state.dateFrom ? '开始' : '结束'}日期`;
    $('#calendarSelectionHint').textContent = '继续补充另一端日期，或直接查看当前条件';
  } else {
    const periodLabels = {day:'按单日查看', week:'按本周查看', month:'按本月查看', all:'查看全部产品'};
    $('#dateRangeStatus').textContent = `当前${periodLabels[state.period]}`;
    $('#calendarSelectionHint').textContent = '点击日期即可查看当天事件';
  }
  $$('.date-presets button').forEach(button => {
    const preset = button.dataset.rangePreset;
    const today = todayKey();
    const end = preset === 'next7' ? dateKey(addDays(today, 6)) : preset === 'next30' ? dateKey(addDays(today, 29)) : today;
    button.classList.toggle('active', state.dateFrom === today && state.dateTo === end);
  });
}

function ensureCalendarShows(date) {
  if (!date) return;
  const windowEnd = dateKey(addDays(state.calendarStart, 27));
  if (date < state.calendarStart || date > windowEnd) {
    state.calendarStart = dateKey(startOfWeek(addDays(date, -7)));
  }
}

function applyDateState({reframe = false} = {}) {
  [state.dateFrom, state.dateTo] = normalizedRange(state.dateFrom, state.dateTo);
  if (reframe) ensureCalendarShows(state.dateFrom || state.dateTo);
  state.page = 1;
  updateDateWorkbench();
  updatePeriodUI();
  updateFilterPills();
  loadCalendar();
  loadGames();
}

function selectCalendarDate(date) {
  if ((state.dateFrom && !state.dateTo) || (!state.dateFrom && state.dateTo)) {
    [state.dateFrom, state.dateTo] = normalizedRange(state.dateFrom || date, state.dateTo || date);
  } else if (state.dateFrom && state.dateFrom === state.dateTo && date !== state.dateFrom) {
    [state.dateFrom, state.dateTo] = normalizedRange(state.dateFrom, date);
  } else {
    state.dateFrom = date;
    state.dateTo = date;
  }
  applyDateState();
}

function setDatePreset(preset) {
  const today = todayKey();
  state.dateFrom = today;
  state.dateTo = preset === 'next7' ? dateKey(addDays(today, 6)) : preset === 'next30' ? dateKey(addDays(today, 29)) : today;
  state.calendarStart = dateKey(startOfWeek(addDays(today, -7)));
  applyDateState();
}

function clearDateRange({reload = true} = {}) {
  state.dateFrom = '';
  state.dateTo = '';
  state.page = 1;
  updateDateWorkbench();
  updatePeriodUI();
  updateFilterPills();
  if (reload) { loadCalendar(); loadGames(); }
}

function updatePeriodUI() {
  const hasRange = Boolean(state.dateFrom || state.dateTo);
  const labels = {day: state.anchor === todayKey() ? '今日新游' : `${fmtDate(state.anchor)} 新游`, week:'本周新游', month:'本月新游', all:'所有新游'};
  if (hasRange) {
    const from = state.dateFrom ? fmtDate(state.dateFrom) : '最早记录';
    const to = state.dateTo ? fmtDate(state.dateTo) : '最新记录';
    $('#catalogTitle').textContent = state.dateFrom === state.dateTo ? `${from} 新游` : `${from} — ${to} 新游`;
  } else {
    $('#catalogTitle').textContent = labels[state.period];
  }
  $$('.metric').forEach(button => button.classList.toggle('primary', !hasRange && button.dataset.period === state.period));
}

function updateFilterPills() {
  const sourceLabels = Object.fromEntries((state.filters?.sources || []).map(x => [x.key, x.note ? `${x.label}（${x.note}）` : x.label]));
  const eventLabels = Object.fromEntries((state.filters?.event_types || []).map(x => [x.key,x.label]));
  const allEventKeys = (state.filters?.event_types || []).map(x => x.key);
  const excludedEvents = allEventKeys.filter(key => !state.events.has(key));
  let eventFilterLabel = '';
  if (excludedEvents.length === 1 && excludedEvents[0] === 'first_seen') {
    eventFilterLabel = '已排除：首次采集发现';
  } else if (excludedEvents.length === 0 && allEventKeys.includes('first_seen')) {
    eventFilterLabel = '事件类型：全部（含首次采集发现）';
  } else if (excludedEvents.length && excludedEvents.length <= 3) {
    eventFilterLabel = `已排除：${excludedEvents.map(key => eventLabels[key]).filter(Boolean).join('、')}`;
  } else if (allEventKeys.length) {
    eventFilterLabel = `事件类型：已选 ${state.events.size}/${allEventKeys.length}`;
  }
  const dateLabel = state.dateFrom || state.dateTo
    ? (state.dateFrom === state.dateTo ? fmtDate(state.dateFrom) : `${state.dateFrom ? fmtDate(state.dateFrom) : '最早'} — ${state.dateTo ? fmtDate(state.dateTo) : '最新'}`)
    : '';
  const values = [...state.sources].map(x => sourceLabels[x]).concat(eventFilterLabel, state.category, state.developer, dateLabel, state.followed ? '只看已关注' : '').filter(Boolean);
  $('#activeFilters').innerHTML = values.map(x => `<span class="active-pill">${escapeHTML(x)}</span>`).join('');
  $('#activeFilterCount').textContent = values.length + (state.q ? 1 : 0);
}

function resetFilters() {
  state.sources.clear(); state.events = defaultEventTypes(); state.category = ''; state.developer = ''; state.q = ''; state.dateFrom = ''; state.dateTo = ''; state.followed = false; state.page = 1;
  $$('.source-filter-list:not(.event-filter-list) .source-check input').forEach(x => x.checked = false);
  $$('.event-filter-list .event-check input').forEach(x => { x.checked = state.events.has(x.value); });
  $('#categorySelect').value = ''; $('#developerSelect').value = ''; $('#searchInput').value = '';
  $('#followedOnly').classList.remove('active');
  updateDateWorkbench(); updatePeriodUI(); updateFilterPills(); loadSummary(); loadCalendar(); loadGames();
}

function bindUI() {
  $$('.metric').forEach(button => button.addEventListener('click', () => {
    state.period = button.dataset.period; state.anchor = todayKey(); state.dateFrom = ''; state.dateTo = ''; state.page = 1;
    updateDateWorkbench(); updatePeriodUI(); updateFilterPills(); loadCalendar(); loadGames();
  }));
  let searchTimer;
  $('#searchInput').addEventListener('input', event => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { state.q = event.target.value.trim(); state.page = 1; updateFilterPills(); loadCalendar(); loadGames(); }, 260); });
  $('#categorySelect').addEventListener('change', e => { state.category = e.target.value; state.page = 1; updateFilterPills(); loadCalendar(); loadGames(); });
  $('#developerSelect').addEventListener('change', e => { state.developer = e.target.value; state.page = 1; updateFilterPills(); loadCalendar(); loadGames(); });
  $('#dateFrom').addEventListener('change', e => { state.dateFrom = e.target.value; applyDateState({reframe:true}); });
  $('#dateTo').addEventListener('change', e => { state.dateTo = e.target.value; applyDateState({reframe:true}); });
  $('#applyDateRange').addEventListener('click', () => {
    state.dateFrom = $('#dateFrom').value;
    state.dateTo = $('#dateTo').value;
    if (state.dateFrom && !state.dateTo) state.dateTo = state.dateFrom;
    if (!state.dateFrom && state.dateTo) state.dateFrom = state.dateTo;
    applyDateState({reframe:true});
  });
  $('#clearDateRange').addEventListener('click', () => clearDateRange());
  $$('.date-presets button').forEach(button => button.addEventListener('click', () => setDatePreset(button.dataset.rangePreset)));
  $('#prevCalendar').addEventListener('click', () => { state.calendarStart = dateKey(addDays(state.calendarStart, -28)); loadCalendar(); });
  $('#nextCalendar').addEventListener('click', () => { state.calendarStart = dateKey(addDays(state.calendarStart, 28)); loadCalendar(); });
  $('#todayCalendar').addEventListener('click', () => { state.calendarStart = dateKey(startOfWeek(addDays(new Date(), -7))); loadCalendar(); });
  $('#sortSelect').addEventListener('change', e => { state.sort = e.target.value; loadGames(); });
  $$('.dimension-switch button').forEach(button => button.addEventListener('click', () => setDimension(button.dataset.dimension)));
  $('#clearFilters').addEventListener('click', resetFilters);
  $('#prevPage').addEventListener('click', () => { if (state.page > 1) { state.page--; loadGames(); scrollTo({top: $('.catalog-section').offsetTop, behavior:'smooth'}); } });
  $('#nextPage').addEventListener('click', () => { state.page++; loadGames(); scrollTo({top: $('.catalog-section').offsetTop, behavior:'smooth'}); });
  $('#gridView').addEventListener('click', () => { state.view='grid'; $('#gameGrid').classList.remove('list-view'); $('#gridView').classList.add('active'); $('#listView').classList.remove('active'); });
  $('#listView').addEventListener('click', () => { state.view='list'; $('#gameGrid').classList.add('list-view'); $('#listView').classList.add('active'); $('#gridView').classList.remove('active'); });
  $('#mobileFilter').addEventListener('click', () => { $('#filtersPanel').classList.add('open'); $('#filterBackdrop').classList.add('open'); });
  $('#accountButton').addEventListener('click', openAccountDrawer);
  $('#closeAccount').addEventListener('click', closeOverlays);
  $('#followedOnly').addEventListener('click', () => {
    state.followed = !state.followed; state.page = 1;
    $('#followedOnly').classList.toggle('active', state.followed);
    updateFilterPills(); loadGames();
  });
  $('#loginForm').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true; setFormMessage('#loginMessage', '正在验证账号…');
    try {
      await api('/api/auth/login', {}, {method:'POST', body:{username:$('#loginUsername').value.trim(), password:$('#loginPassword').value}});
      $('#loginPassword').value = '';
      await loadAuth(); await loadGames();
      setFormMessage('#loginMessage');
    } catch (error) {
      setFormMessage('#loginMessage', error.message, 'error');
    } finally { button.disabled = false; }
  });
  $('#logoutButton').addEventListener('click', async () => {
    try { await api('/api/auth/logout', {}, {method:'POST', csrf:true}); } catch (_) {}
    state.auth = {user:null, csrfToken:'', favoriteCount:0, permissions:{manage_users:false, manage_admins:false}};
    state.followed = false; renderAuth(); closeOverlays(); updateFilterPills(); await loadGames();
  });
  $$('.account-tabs button').forEach(button => button.addEventListener('click', () => activateAccountTab(button.dataset.accountTab)));
  $('#createApiKeyForm').addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true; setFormMessage('#apiKeyMessage', '正在生成安全密钥…');
    try {
      const data = await api('/api/account/api-keys', {}, {method:'POST', csrf:true, body:{name:$('#apiKeyName').value.trim()}});
      $('#generatedApiKeyValue').textContent = data.api_key;
      $('#generatedApiKey').hidden = false;
      setFormMessage('#apiKeyMessage', '密钥已生成。离开页面后将无法再次查看完整值。', 'success');
      await loadApiKeys();
    } catch (error) { setFormMessage('#apiKeyMessage', error.message, 'error'); }
    finally { button.disabled = false; }
  });
  $('#copyApiKey').addEventListener('click', async () => {
    const value = $('#generatedApiKeyValue').textContent;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      $('#copyApiKey').textContent = '已复制';
      setTimeout(() => { $('#copyApiKey').textContent = '复制密钥'; }, 1600);
    } catch (_) { setFormMessage('#apiKeyMessage', '浏览器未允许复制，请手动选择密钥。', 'error'); }
  });
  $('#apiKeyList').addEventListener('click', event => {
    const button = event.target.closest('[data-revoke-api-key]');
    if (button) revokeApiKey(button);
  });
  $('#profileForm').addEventListener('submit', async event => {
    event.preventDefault();
    const newPassword = $('#newPassword').value;
    if (newPassword && newPassword !== $('#confirmPassword').value) {
      setFormMessage('#profileMessage', '两次输入的新密码不一致。', 'error'); return;
    }
    const payload = {display_name:$('#profileDisplayInput').value.trim()};
    if (newPassword) { payload.current_password = $('#currentPassword').value; payload.new_password = newPassword; }
    const button = event.currentTarget.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      const data = await api('/api/account/profile', {}, {method:'PATCH', csrf:true, body:payload});
      state.auth.user = data.user; state.auth.permissions = data.permissions;
      $('#currentPassword').value = ''; $('#newPassword').value = ''; $('#confirmPassword').value = '';
      renderAuth(); setFormMessage('#profileMessage', '个人资料已保存。', 'success');
    } catch (error) { setFormMessage('#profileMessage', error.message, 'error'); }
    finally { button.disabled = false; }
  });
  $('#createUserForm').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {
      username:$('#newUsername').value.trim(), display_name:$('#newDisplayName').value.trim(),
      password:$('#newUserPassword').value, role:state.auth.permissions.manage_admins ? $('#newUserRole').value : 'user',
    };
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try {
      await api('/api/admin/users', {}, {method:'POST', csrf:true, body:payload});
      form.reset(); $('#newUserRole').value = 'user';
      setFormMessage('#createUserMessage', '账号已创建。', 'success'); await loadUsers();
    } catch (error) { setFormMessage('#createUserMessage', error.message, 'error'); }
    finally { button.disabled = false; }
  });
  $('#userList').addEventListener('click', event => {
    const button = event.target.closest('[data-user-action]');
    if (!button) return;
    const card = button.closest('.user-card');
    if (button.dataset.userAction === 'edit') card.classList.toggle('editing');
    if (button.dataset.userAction === 'delete') deleteManagedUser(button);
  });
  $('#userList').addEventListener('submit', event => { event.preventDefault(); saveManagedUser(event.target); });
  $('#closeDrawer').addEventListener('click', closeOverlays); $('#filterBackdrop').addEventListener('click', closeOverlays);
  $('#lightboxClose').addEventListener('click', closeLightbox);
  $('#lightboxPrev').addEventListener('click', () => moveLightbox(-1));
  $('#lightboxNext').addEventListener('click', () => moveLightbox(1));
  $('#imageLightbox').addEventListener('click', event => { if (event.target === event.currentTarget) closeLightbox(); });
  document.addEventListener('keydown', event => {
    if (!$('#imageLightbox').hidden) {
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault(); moveLightbox(event.key === 'ArrowLeft' ? -1 : 1);
      }
      if (event.key === 'Escape') { event.preventDefault(); closeLightbox(); }
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); $('#searchInput').focus(); }
    if (event.key === 'Escape') closeOverlays();
  });
}

async function boot() {
  bindUI(); updateDimensionUI(); updateDateWorkbench(); updatePeriodUI();
  await Promise.all([loadAuth(), loadFilters(), loadSummary(), loadCalendar()]);
  updateFilterPills(); await loadGames();
}
boot();
