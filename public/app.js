/* FieldPulse browser client. Server authorization is authoritative; UI is only convenience. */
"use strict";
const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));
const state = {user:null, page:"overview", tickets:[], sites:[], acts:[], report:null, selected:null, timeline:[], suggestions:[], filter:"", ticketStatus:"", ticketPriority:"", ticketSort:"newest", ticketPage:1, ticketPageSize:20, siteFilter:"", eventStream:null, localTasks:[]};
const UI_PREFS_KEY = "FieldPulseUIPreferencesV4";
const prefs = (() => {try{return JSON.parse(localStorage.getItem(UI_PREFS_KEY)||"{}");}catch{return {};}})();
const ui = {theme:prefs.theme==="dark"?"dark":"light", density:prefs.density==="compact"?"compact":"comfortable"};
function savePrefs(){try{localStorage.setItem(UI_PREFS_KEY,JSON.stringify(ui));}catch{}}
function applyAppearance(){
  document.documentElement.dataset.theme=ui.theme;
  document.documentElement.classList.toggle("density-compact",ui.density==="compact");
  document.querySelectorAll("[data-theme]").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.theme===ui.theme)));
  document.querySelectorAll("[data-density]").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.density===ui.density)));
}
const ICON_PATH = {
 overview:'<rect x="3" y="3" width="7" height="9" rx="1.4"/><rect x="14" y="3" width="7" height="5" rx="1.4"/><rect x="14" y="12" width="7" height="9" rx="1.4"/><rect x="3" y="16" width="7" height="5" rx="1.4"/>',
 tickets:'<path d="M8 6h13M8 12h13M8 18h13"/><path d="M3.5 6h.01M3.5 12h.01M3.5 18h.01" stroke-width="3" stroke-linecap="round"/>',
 sites:'<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M9 7h2m2 0h2M9 11h2m2 0h2M9 15h6m-5 6v-4h4v4"/>',
 mobile:'<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>',
 acts:'<path d="M7 3h8l4 4v14H5V3h2"/><path d="M14 3v5h5M9 13h6M9 17h6"/>',
 reports:'<path d="M4 20V11m6 9V5m6 15v-9m4 9V8"/><path d="M2 20h20"/>',
 inbox:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 14h5l2 3h4l2-3h5"/>',
 admin:'<circle cx="12" cy="12" r="3"/><path d="m19 13 2-1-2-1-.5-2 1-2-2-2-2 1-2-.5-1-2-1 2-2 .5-2-1-2 2 1 2-.5 2-2 1 2 1 .5 2-1 2 2 2 2-1 2 .5 1 2 1-2 2-.5 2 1 2-2-1-2z"/>'
};
const glyph = name => '<svg class="nav-glyph" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'+ICON_PATH[name]+'</svg>';

const ROLE = {admin:"Администратор",manager:"Руководитель",dispatcher:"Диспетчер",engineer:"Инженер",client:"Клиент"};
const STATUS = {new:"Новая",triage:"Триаж",assigned:"Назначена",accepted:"Принята",in_progress:"В работе",approval:"Согласование",returned:"Возврат",closed:"Закрыта",cancelled:"Отменена"};
const TITLE = {overview:"Обзор",tickets:"Заявки",sites:"Объекты",mobile:"Мобильное рабочее место",acts:"Акты выполненных работ",reports:"Аналитика",inbox:"Уведомления",admin:"Администрирование"};
const CHECKS = {electrical:["isolation","voltage","result"],hvac:["filter","pressure","result"],low_voltage:["connection","signal","result"]};
const CHECK_LABEL = {isolation:"Безопасное отключение",voltage:"Проверка напряжения",result:"Проверка результата",filter:"Состояние фильтра",pressure:"Давление системы",connection:"Подключение",signal:"Проверка сигнала",inspection:"Осмотр"};
const safe = value => String(value == null ? "" : value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const short = id => safe(String(id || "").slice(0,8).toUpperCase());
const date = (v,zone) => {if(!v) return "—";try{return new Intl.DateTimeFormat("ru-RU",{dateStyle:"short",timeStyle:"short",timeZone:zone||"Europe/Moscow"}).format(new Date(v));}catch{return "—";}};
const status = s => '<span class="status '+safe(s)+'">'+safe(STATUS[s]||s)+'</span>';
const priority = p => '<span class="priority '+safe((p||"").toLowerCase())+'">'+safe(p)+'</span>';
const toast = (message,error=false) => {const el=$("#toast");el.textContent=message;el.className="toast visible"+(error?" error":"");clearTimeout(window.toastTimer);window.toastTimer=setTimeout(()=>el.classList.remove("visible"),4200);};
const csrf = () => {const entry=document.cookie.split("; ").find(x=>x.startsWith("fp_csrf="));return entry?decodeURIComponent(entry.slice(8)):"";};
async function api(path, options={}){
  const headers={...options.headers};
  if(options.body && !(options.body instanceof FormData)){headers["Content-Type"]="application/json";if(typeof options.body!=="string") options.body=JSON.stringify(options.body);}
  if(options.method && !["GET","HEAD"].includes(options.method.toUpperCase()))headers["X-CSRF-Token"]=csrf();
  const response=await fetch("/api/v1"+path,{...options,headers,credentials:"same-origin"});
  const payload=await response.json().catch(()=>null);
  if(!response.ok){const detail=payload&&payload.details;const message=(detail && detail.code?detail.code+": ":"")+(payload&&payload.message||"Ошибка соединения");const error=new Error(message);error.detail=detail;error.status=response.status;throw error;}
  return payload;
}
function viewAuth(){const logged=Boolean(state.user);$("#login").hidden=logged;$("#shell").hidden=!logged;}
function navItems(){
  if(state.user.role==="engineer")return ["overview","tickets","mobile","acts"];
  if(state.user.role==="client")return ["overview","tickets","sites","acts"];
  return state.user.role==="admin" ? ["overview","tickets","sites","reports","acts","inbox","admin"] : ["overview","tickets","sites","reports","acts","inbox"];
}
function setIdentity(){
  $("#user-name").textContent=state.user.name;$("#user-role").textContent=ROLE[state.user.role]||state.user.role;
  $("#user-avatar").textContent=state.user.name.split(" ").map(v=>v[0]).slice(0,2).join("").toUpperCase();
  $("#today-label").textContent=new Intl.DateTimeFormat("ru-RU",{day:"numeric",month:"long",year:"numeric"}).format(new Date());
  $("#navigation").innerHTML=navItems().map(p=>'<button type="button" class="nav-item '+(p===state.page?"active":"")+'" data-page="'+p+'" '+(p===state.page?'aria-current="page"':"")+'><span class="nav-icon">'+glyph(p)+'</span>'+TITLE[p]+'</button>').join("");
  $("#breadcrumb-current").textContent=TITLE[state.page];
  applyAppearance();
}
function pageHead(kicker,title,description,actions=""){
  return '<div class="page-head"><div><div class="section-kicker">'+kicker+'</div><h1>'+title+'</h1><p>'+description+'</p></div><div class="page-actions">'+actions+'</div></div>';
}
function stat(label,val,foot,icon,highlight=false){
  return '<div class="stat-card '+(highlight?"highlight":"")+'"><div class="stat-top"><span class="stat-label">'+label+'</span><span class="stat-icon">'+icon+'</span></div><div class="stat-value">'+safe(val)+'</div><div class="stat-foot">'+foot+'</div></div>';
}
function panel(title,sub,body,extra=""){
  return '<div class="panel"><div class="panel-header"><div><h2>'+title+'</h2>'+(sub?'<p>'+sub+'</p>':"")+'</div>'+extra+'</div>'+body+'</div>';
}
function slaDeadline(t){
  if(["closed","cancelled"].includes(t.status))return '<span class="sla-state">Завершена</span>';
  if(!t.resolution_deadline)return '<span class="muted">Срок не указан</span>';
  const diff=new Date(t.resolution_deadline).getTime()-Date.now();
  const level=diff<0?"overdue":diff<=60*60*1000?"due-soon":"";
  const label=diff<0?"Просрочена":diff<=60*60*1000?"Срок близко":"В пределах SLA";
  return '<span class="sla-state '+level+'">'+label+'</span>';
}
function table(items){
  if(!items.length)return '<div class="empty"><div class="empty-icon">◇</div><strong>Заявки не найдены</strong><span>Попробуйте изменить фильтры или поисковый запрос.</span></div>';
  return '<div class="table-wrap"><table class="table"><thead><tr><th scope="col">ЗАЯВКА</th><th scope="col">ОБЪЕКТ / ОБОРУДОВАНИЕ</th><th scope="col">ПРИОРИТЕТ</th><th scope="col">СТАТУС</th><th scope="col">ИСПОЛНИТЕЛЬ</th><th scope="col">СРОК SLA</th></tr></thead><tbody>'+
    items.map(t=>'<tr><td><button type="button" class="ticket-cell" data-ticket="'+safe(t.id)+'" aria-label="Открыть заявку '+short(t.id)+'">#'+short(t.id)+'</button><div class="id-cell">'+date(t.created_at,t.timezone)+'</div></td>'+
    '<td><div class="title-cell">'+safe(t.site)+'</div><small class="muted">'+safe(t.asset)+'</small></td><td>'+priority(t.priority)+'</td><td>'+status(t.status)+'</td>'+
    '<td>'+safe(t.assignee||"Не назначен")+'</td><td>'+date(t.resolution_deadline,t.timezone)+
    '<div class="sla-digest">'+slaDeadline(t)+'</div></td></tr>').join("")+'</tbody></table></div>';
}

function overview(){
  const r=state.report;
  const all=state.tickets;
  const mine=state.user.role==="engineer";
  const active=all.filter(t=>!["closed","cancelled"].includes(t.status));
  const headline=state.user.role==="client"?"Контроль обслуживания":mine?"Мои выездные задачи":"Сервис под контролем";
  const header=pageHead("ОПЕРАЦИОННЫЙ ОБЗОР",headline,"Актуальные обращения, исполнители и сроки на одном экране.",
    mine?'<button class="btn btn-primary" data-page="mobile">↗ Перейти к заданиям</button>':
    '<button class="btn btn-primary" data-create-ticket>＋ Создать заявку</button>');
  const summary='<div class="stats">'+
    stat("Всего заявок",r?r.total:all.length,"В доступном вам реестре","▣")+
    stat("Открытые",r?r.open:active.length,"Требуют выполнения","◷")+
    stat("Закрыто",r?r.closed:all.filter(t=>t.status==="closed").length,"Работы завершены","✓")+
    stat(r?"Нарушения SLA":"Не назначены",r?r.breached_percent+"%":active.filter(t=>!t.assignee_id).length,
      r?"Доля заявок с нарушением":"Ожидают исполнителя","↗",true)+'</div>';
  const upcoming=active.filter(t=>t.resolution_deadline).sort((a,b)=>new Date(a.resolution_deadline)-new Date(b.resolution_deadline)).slice(0,6);
  const watchlist=panel("Под контролем","Ближайшие сроки из доступных загруженных заявок",
    '<div class="watchlist">'+(upcoming.length?upcoming.map(t=>
    '<button type="button" class="watchlist-row" data-ticket="'+safe(t.id)+'"><span class="watchlist-head"><b>#'+short(t.id)+'</b>'+
    priority(t.priority)+'</span><span class="watchlist-description">'+safe(t.site)+' · '+safe(t.asset)+'</span>'+
    '<span class="watchlist-date">'+date(t.resolution_deadline,t.timezone)+' · '+slaDeadline(t)+'</span></button>').join(""):
    '<div class="empty">Нет активных заявок со сроком SLA.</div>')+'</div>');
  const recent=panel("Последние обращения","Последние изменения в доступном реестре",table(all.slice(0,7)),
    '<button class="btn btn-outline" data-page="tickets">Весь реестр ↗</button>');
  const steps=["new","triage","assigned","accepted","in_progress","approval","closed"];
  const total=Math.max(1,all.length);
  const distribution=panel("Этапы обслуживания","Распределение загруженных заявок по статусам",
    '<div class="panel-body"><div class="activity">'+steps.map(key=>{
      const count=all.filter(t=>t.status===key).length;
      return '<div class="activity-item"><span class="activity-dot">◦</span><div><b>'+STATUS[key]+'<span class="activity-total">'+count+'</span></b>'+
      '<div class="progress-meter" role="progressbar" aria-label="'+STATUS[key]+'" aria-valuemin="0" aria-valuemax="'+all.length+'" aria-valuenow="'+count+'"><span style="width:'+Math.round(count/total*100)+'%"></span></div></div></div>';
    }).join("")+'</div></div>');
  return header+summary+
    '<div class="intro-banner"><div><span class="eyebrow">FIELD OPERATIONS / V0.4</span><h2>Вся работа — в едином потоке</h2>'+
    '<p>Следите за изменениями заявок, документами, назначениями и SLA без переключения между системами.</p></div>'+
    '<button class="btn btn-primary" data-page="'+(mine?"mobile":"tickets")+'">Открыть рабочий список ↗</button></div>'+
    '<div class="layout-two">'+recent+watchlist+'</div>'+distribution;
}

function filteredTickets(){
  const match=state.filter.trim().toLocaleLowerCase("ru");
  const filtered=state.tickets.filter(t=>
    (!match||[t.id,t.site,t.asset,t.description,t.assignee||""].some(x=>String(x||"").toLocaleLowerCase("ru").includes(match)))&&
    (!state.ticketStatus||t.status===state.ticketStatus)&&
    (!state.ticketPriority||t.priority===state.ticketPriority));
  const p={P1:1,P2:2,P3:3,P4:4};
  return filtered.sort((a,b)=>{
    if(state.ticketSort==="priority")return (p[a.priority]||9)-(p[b.priority]||9)||new Date(a.resolution_deadline)-new Date(b.resolution_deadline);
    if(state.ticketSort==="deadline")return new Date(a.resolution_deadline||"9999-01-01")-new Date(b.resolution_deadline||"9999-01-01");
    return new Date(b.created_at)-new Date(a.created_at);
  });
}
function ticketResults(){
  const all=filteredTickets(),total=all.length;
  const pages=Math.max(1,Math.ceil(total/state.ticketPageSize));
  state.ticketPage=Math.max(1,Math.min(pages,state.ticketPage));
  const from=(state.ticketPage-1)*state.ticketPageSize;
  const page=all.slice(from,from+state.ticketPageSize);
  const toolbar='<div class="table-toolbar"><div class="table-group"><span class="result-count">'+(total?from+1:0)+'–'+Math.min(total,from+state.ticketPageSize)+' из '+total+' загруженных</span>'+
    '<span class="muted">· до 500 заявок в выдаче API</span></div><div class="toolbar-actions">'+
    '<button type="button" class="btn btn-ghost" data-density="comfortable" aria-pressed="'+(ui.density==="comfortable")+'" title="Обычная плотность">Обычно</button>'+
    '<button type="button" class="btn btn-ghost" data-density="compact" aria-pressed="'+(ui.density==="compact")+'" title="Компактная таблица">Плотно</button>'+
    '<button type="button" class="btn btn-outline" data-ticket-page="'+(state.ticketPage-1)+'" '+(state.ticketPage===1?"disabled":"")+' aria-label="Предыдущая страница">‹</button>'+
    '<span class="result-count">'+state.ticketPage+' / '+pages+'</span>'+
    '<button type="button" class="btn btn-outline" data-ticket-page="'+(state.ticketPage+1)+'" '+(state.ticketPage===pages?"disabled":"")+' aria-label="Следующая страница">›</button></div></div>';
  return table(page)+toolbar;
}
function updateTicketResults(){
  const element=$("#ticket-table");if(!element)return;
  element.innerHTML=ticketResults();
  const count=$("#ticket-result-count");if(count)count.textContent=filteredTickets().length+" найдено";
  applyAppearance();
}
function ticketsPage(){
  return pageHead("УПРАВЛЕНИЕ ЗАЯВКАМИ","Реестр обращений","Поиск, актуальные сроки и действия по доступным вашей роли заявкам.",
    state.user.role==="engineer"?"":'<button class="btn btn-primary" data-create-ticket>＋ Создать заявку</button>')+
    '<div class="panel"><div class="filters"><input class="search" id="ticket-search" type="search" aria-label="Поиск заявок" placeholder="Номер, объект, оборудование, описание" value="'+safe(state.filter)+'">'+
    '<select id="ticket-status-filter" aria-label="Статус заявки"><option value="">Все статусы</option>'+Object.keys(STATUS).map(s=>
    '<option value="'+s+'" '+(s===state.ticketStatus?"selected":"")+'>'+STATUS[s]+'</option>').join("")+'</select>'+
    '<select id="ticket-priority-filter" aria-label="Приоритет"><option value="">Все приоритеты</option>'+["P1","P2","P3","P4"].map(s=>
    '<option value="'+s+'" '+(s===state.ticketPriority?"selected":"")+'>'+s+'</option>').join("")+'</select>'+
    '<select id="ticket-sort" aria-label="Порядок сортировки">'+[
    ["newest","Сначала новые"],["deadline","Ближайший SLA"],["priority","Сначала P1"]
    ].map(([value,label])=>'<option value="'+value+'" '+(state.ticketSort===value?"selected":"")+'>'+label+'</option>').join("")+'</select>'+
    '<span id="ticket-result-count" class="result-count">'+filteredTickets().length+' найдено</span></div>'+
    '<div id="ticket-table">'+ticketResults()+'</div></div>';
}

function siteCards(){
  const query=state.siteFilter.trim().toLocaleLowerCase("ru");
  const sites=state.sites.filter(s=>!query||[s.name,s.city,s.area,s.timezone].some(x=>String(x||"").toLocaleLowerCase("ru").includes(query)));
  return (sites.length?sites.map(s=>
    '<article class="site-card"><div class="site-card-icon">'+glyph("sites")+'</div><h3>'+safe(s.name)+'</h3><p>'+safe(s.city)+' / '+safe(s.area)+'</p>'+
    '<div class="site-card-foot"><span>'+safe(s.timezone)+'</span><span>Объект обслуживания</span></div></article>').join(""):
    '<div class="empty"><div class="empty-icon">⌕</div><strong>Объекты не найдены</strong><span>Измените поисковый запрос.</span></div>');
}
function sitesPage(){
  return pageHead("ОБЪЕКТЫ И ОБОРУДОВАНИЕ","Обслуживаемые объекты","Все доступные вашей роли объекты, районы и рабочие часовые пояса.")+
    '<div class="panel site-filters"><div class="filters">'+
    '<input id="site-search" class="search" type="search" aria-label="Поиск объектов" value="'+safe(state.siteFilter)+'" placeholder="Найти объект, город или район">'+
    '<span class="result-count" id="site-count">'+state.sites.length+' объектов загружено</span></div></div>'+
    '<div class="site-grid" id="site-grid">'+siteCards()+'</div>';
}

function actsPage(){
  const records=state.acts;
  const pending=records.filter(x=>x.status==="pending");
  const accepted=records.filter(x=>x.status==="accepted");
  const rejected=records.filter(x=>x.status==="rejected");
  const drafts=records.filter(x=>x.status==="draft");
  const summary='<div class="stats">'+
    stat("Всего редакций",records.length,"Доступные вашей роли акты","▣")+
    stat("На согласовании",pending.length,"Ожидают решения","◷",true)+
    stat("Принято",accepted.length,"Подтверждённые акты","✓")+
    stat("Возвращено",rejected.length,"Для повторной доработки","↶")+'</div>';
  const note='<div class="intro-banner"><div><h2>Каждое решение имеет историю</h2>'+
    '<p>Возврат создаёт новую редакцию акта. Доступ к документам разграничен по ролям и организациям.</p></div>'+
    '<span class="mobile-state">'+drafts.length+' черновиков</span></div>';
  const listing=records.length?'<div class="table-wrap"><table class="table"><thead><tr><th>АКТ</th><th>ЗАЯВКА</th><th>РЕДАКЦИЯ</th><th>СТАТУС</th><th>ДАТА</th><th>ДЕЙСТВИЕ</th></tr></thead><tbody>'+
    records.map(x=>'<tr><td class="title-cell">#'+short(x.id)+'</td><td><button class="ticket-cell" data-ticket="'+safe(x.ticket_id)+'">#'+short(x.ticket_id)+'</button></td>'+
    '<td>v'+x.version+'</td><td>'+status(x.status)+'</td><td>'+date(x.created_at)+'</td>'+
    '<td><button class="btn btn-outline" data-act="'+safe(x.id)+'">Открыть акт ↗</button></td></tr>').join("")+'</tbody></table></div>':
    '<div class="empty"><div class="empty-icon">▣</div><strong>Акты пока не созданы</strong><span>После выполнения работ они появятся здесь.</span></div>';
  return pageHead("ДОКУМЕНТООБОРОТ","Акты и согласование","Редакции, результаты работ и история согласования.")+
    summary+note+panel("Журнал актов",records.length+" доступных редакций",listing);
}

function reportsPage(){
  const r=state.report||{total:0,open:0,breached_percent:0,repeat_percent:0,load:[]};
  const load=[...(r.load||[])].sort((a,b)=>b.count-a.count).slice(0,18);const max=Math.max(...load.map(a=>a.count),1);
  const sites=[...(state.siteReport?.sites||[])].sort((a,b)=>b.breach_count-a.breach_count||b.total-a.total).slice(0,30);
  const ops=state.operations||{};
  return pageHead("СЕРВИСНАЯ АНАЛИТИКА","Качество под контролем","Реальные показатели заявок, нарушений и текущей загрузки.")+
    '<div class="stats">'+stat("Время реакции p50",r.reaction_p50_minutes??"—","минут","◷")+
    stat("Время реакции p95",r.reaction_p95_minutes??"—","минут","◴")+
    stat("Нарушения SLA",r.breached_percent+"%","Доля обращений","↯",true)+
    stat("Повторные заявки",r.repeat_percent+"%","За последние 24 часа","↻")+'</div>'+
    '<div class="layout-two">'+panel("Нагрузка на инженеров","Текущие незакрытые назначения",
    '<div class="panel-body"><div class="responsive-chart">'+load.map(x=>
    '<div class="chart-col" title="'+safe(x.name)+': '+x.count+'"><div class="chart-bar" style="--h:'+Math.max(3,Math.round(x.count/max*160))+'px"></div><label>'+safe(x.name.replace("Инженер ","#"))+'</label></div>').join("")+'</div></div>')+
    panel("Состояние данных","Показатели, рассчитанные по базе",
    '<div class="panel-body"><div class="activity">'+[
      ["Обращений всего",r.total],["Активных заявок",r.open],["С нарушениями",r.breached_count],["Повторных обращений",r.repeat_count]
    ].map(x=>'<div class="activity-item"><span class="activity-dot">↗</span><div><b>'+x[0]+'</b><small>'+x[1]+'</small></div></div>').join("")+'</div></div>')+'</div>'+
    panel("SLA по объектам","Список приоритетных объектов с нарушениями",'<div class="table-wrap"><table class="table"><thead><tr><th>ОБЪЕКТ</th><th>ЗАЯВКИ</th><th>НАРУШЕНИЯ</th><th>РЕАКЦИЯ P95</th></tr></thead><tbody>'+
      sites.map(s=>'<tr><td>'+safe(s.site)+'</td><td>'+s.total+'</td><td>'+s.breach_count+' ('+s.breach_percent+'%)</td><td>'+safe(s.reaction_p95_minutes??"—")+' мин</td></tr>').join("")+'</tbody></table></div>')+
    panel("Операционное состояние","Исторические события и локальный mock inbox",'<div class="panel-body"><div class="activity">'+
      [["Активные паузы SLA",ops.active_sla_pauses],["Незавершённые заявки",ops.open_tickets],["Зарегистрированные нарушения",ops.recorded_breach_events],["Сообщения без внешней доставки",ops.pending_mock_notifications]].map(x=>
      '<div class="activity-item"><span class="activity-dot">◉</span><div><b>'+x[0]+'</b><small>'+safe(x[1]??"—")+'</small></div></div>').join("")+'</div></div>');
}
function inboxPage(){
  const notifications=state.notifications||[];
  return pageHead("МОНИТОРИНГ И ЭСКАЛАЦИИ","Центр уведомлений","Локальный mock inbox для предупреждений и нарушений SLA.")+
    panel("Журнал отправки",notifications.length+" сообщений",
    '<div class="panel-body"><div class="activity">'+(notifications.length?
      notifications.map(n=>'<div class="activity-item"><span class="activity-dot">◉</span><div><b>'+safe(n.subject)+'</b><small>'+date(n.at)+' / '+safe(n.recipient)+'</small><p style="font-size:11px">'+safe(n.body)+'</p></div></div>').join(""):
      '<div class="empty">Новые уведомления появятся по мере приближения SLA.</div>')+'</div></div>');
}
let previousModalFocus=null;
function modal(title,body,foot="",small=false){
  if(!$("#modal-root").querySelector(".modal"))previousModalFocus=document.activeElement;
  $("#modal-root").innerHTML='<div class="modal-overlay" id="overlay"><section class="modal '+(small?"sm":"")+'" role="dialog" aria-modal="true" aria-labelledby="modal-title">'+
  '<header class="modal-head"><div><div class="section-kicker">FIELD PULSE / WORKSPACE</div><h2 id="modal-title">'+safe(title)+'</h2></div><button type="button" class="modal-close" data-close aria-label="Закрыть окно">×</button></header>'+
  '<div class="modal-content">'+body+'</div>'+(foot?'<div class="modal-foot">'+foot+'</div>':"")+'</section></div>';
  document.body.classList.add("modal-open");
  $("#modal-root .modal-close").focus();
}
function closeModal(){
  const wasOpen=Boolean($("#modal-root .modal"));
  $("#modal-root").innerHTML="";
  document.body.classList.remove("modal-open");
  if(wasOpen&&previousModalFocus?.isConnected)previousModalFocus.focus();
  previousModalFocus=null;
}
async function createTicketModal(){
  if(!state.sites.length)state.sites=await api("/sites");
  if(!state.sites.length)return toast("Нет доступных объектов",true);
  const body='<form id="create-ticket-form" class="form-grid">'+
    '<div class="field span"><label>Объект<select id="new-site" name="site_id" required>'+state.sites.map(s=>'<option value="'+safe(s.id)+'">'+safe(s.name)+'</option>').join("")+'</select></label></div>'+
    '<div class="field"><label>Оборудование<select id="new-asset" name="asset_id" required></select></label></div>'+
    '<div class="field"><label>Приоритет<select name="priority"><option>P1</option><option>P2</option><option selected>P3</option><option>P4</option></select></label></div>'+
    '<div class="field span"><label>Тип обращения<input name="kind" value="repair" minlength="2" maxlength="40" required></label></div>'+
    '<div class="field span"><label>Описание проблемы<textarea name="description" minlength="8" maxlength="2000" required placeholder="Опишите неисправность, симптомы и доступ к оборудованию"></textarea></label></div>'+
    '<div class="field span"><label>Фотография (необязательно, до 10 МБ)<input name="photo" type="file" accept="image/png,image/jpeg,image/webp"></label></div>'+
    '<div class="field span"><button class="btn btn-primary btn-wide" type="submit">Создать заявку <span>↗</span></button></div></form>';
  modal("Новая заявка",body);
  await loadAssets();
}
async function loadAssets(){
  const select=$("#new-asset");if(!select)return;
  try{const items=await api("/assets?site_id="+encodeURIComponent($("#new-site").value));
    select.innerHTML=items.map(x=>'<option value="'+safe(x.id)+'">'+safe(x.name)+' · '+safe(x.kind)+'</option>').join("");}
  catch(err){toast(err.message,true);}
}
async function details(id){
  const t=await api("/tickets/"+encodeURIComponent(id));state.selected=t;
  const [history,photos,logs,checklist]=await Promise.all([
    api("/tickets/"+encodeURIComponent(id)+"/timeline"),
    api("/tickets/"+encodeURIComponent(id)+"/attachments"),
    api("/tickets/"+encodeURIComponent(id)+"/worklogs"),
    api("/tickets/"+encodeURIComponent(id)+"/checklist"),
  ]);
  state.timeline=history;
  const canStaff=["admin","dispatcher","manager"].includes(state.user.role);
  const isEngineer=state.user.role==="engineer";
  const actions=[];
  function action(label,key,style="btn-outline"){actions.push('<button type="button" class="btn '+style+'" data-task-action="'+key+'">'+label+'</button>');}
  if(canStaff&&t.status==="new")action("Взять в триаж","triage");
  if(canStaff&&["new","triage","returned"].includes(t.status))action("Назначить инженера","assign","btn-primary");
  if(canStaff)action("Сверить SLA","reconcile");
  if(isEngineer&&t.status==="assigned")action("Принять заявку","accept","btn-primary");
  if(isEngineer&&["accepted","returned"].includes(t.status))action("Начать работы","start","btn-primary");
  if(canStaff&&["assigned","accepted","in_progress"].includes(t.status))action("Пауза SLA","pause");
  if(canStaff&&["assigned","accepted","in_progress"].includes(t.status))action("Возобновить SLA","resume");
  if(canStaff&&["new","triage","assigned","accepted","in_progress","returned"].includes(t.status))action("Отменить","cancel","btn-red");
  if(isEngineer&&t.status==="in_progress")action("Оформить акт","submit","btn-primary");
  const gallery=photos.length?
    '<div class="photo-grid">'+photos.map(p=>
      '<a class="photo-frame" target="_blank" rel="noopener noreferrer" href="/api/v1/attachments/'+encodeURIComponent(p.id)+'" aria-label="Открыть фото '+short(p.id)+'">'+
      '<img loading="lazy" alt="Фотография заявки '+short(t.id)+'" src="/api/v1/attachments/'+encodeURIComponent(p.id)+'?thumb=true">'+
      '<span>Фото '+short(p.id)+' · '+Math.round(p.size/1024)+' КиБ</span></a>').join("")+'</div>':
    '<div class="empty empty-compact">Фотографии пока не добавлены.</div>';
  const completedChecks=Object.create(null);
  for(const record of logs)for(const [key,value] of Object.entries(record.checklist||{}))
    if(value===true)completedChecks[key]=true;
  const requiredChecks=checklist.required||[];
  const checksDone=requiredChecks.filter(key=>completedChecks[key]===true).length;
  const checklistView='<div class="checklist-progress" aria-label="Проверок выполнено '+checksDone+' из '+requiredChecks.length+'"><strong>'+
    checksDone+' / '+requiredChecks.length+'</strong><span>проверок подтверждено</span></div>'+
    '<div class="detail-checklist">'+requiredChecks.map(key=>
      '<div class="check-row"><span class="check-mark '+(completedChecks[key]===true?"done":"")+'">'+
      (completedChecks[key]===true?"✓":"○")+'</span><span>'+safe(CHECK_LABEL[key]||key)+'</span></div>').join("")+'</div>';
  const chronology=history.length?history.map(e=>
    '<div class="timeline-entry"><b>'+safe(e.type.replaceAll("_"," "))+'</b><small>'+date(e.at,t.timezone)+'</small>'+
    (e.data&&(e.data.text||e.data.reason)?'<p>'+safe(e.data.text||e.data.reason)+'</p>':"")+'</div>').join(""):
    '<div class="muted">Событий пока нет.</div>';
  const work=logs.length?logs.map(w=>
    '<div class="worklog-entry"><strong>'+safe(w.summary||"Запись о выполненных работах")+'</strong><small>'+date(w.server_at,t.timezone)+
    ' · '+(w.materials||[]).length+' материалов</small></div>').join(""):
    '<p class="muted">Записей о выполненных работах ещё нет.</p>';
  const body='<div class="detail-summary"><div class="sla-digest">'+priority(t.priority)+status(t.status)+
    '<span class="muted">Редакция '+t.version+'</span></div>'+
    '<h3>'+safe(t.description)+'</h3><p>'+safe(t.site)+' · '+safe(t.asset)+'</p>'+
    '<div class="sla-digest">'+slaDeadline(t)+' <small>· Политика '+safe(t.sla.policy_name)+'</small></div></div>'+
    '<div class="detail-meta"><div><small>Создана</small><strong>'+date(t.created_at,t.timezone)+'</strong></div>'+
    '<div><small>Реакция до</small><strong>'+date(t.reaction_deadline,t.timezone)+'</strong></div>'+
    '<div><small>Решение до</small><strong>'+date(t.resolution_deadline,t.timezone)+'</strong></div></div>'+
    '<p class="muted" style="font-size:11px">Часовой пояс: '+safe(t.timezone)+' · Исполнитель: '+safe(t.assignee||"Не назначен")+'</p>'+
    '<div class="detail-buttons">'+actions.join("")+'</div>'+
    '<div class="detail-subgrid"><section><h3 class="subheading">Хронология событий <span>'+history.length+'</span></h3>'+
    '<div class="timeline">'+chronology+'</div></section>'+
    '<section><h3 class="subheading">Фотографии <span>'+photos.length+'</span></h3>'+gallery+
    '<h3 class="subheading">Проверки <span>'+checksDone+'/'+requiredChecks.length+'</span></h3>'+checklistView+
    '<h3 class="subheading">Выполненные работы <span>'+logs.length+'</span></h3>'+work+'</section></div>'+
    '<div class="detail-comment field"><label>Комментарий<textarea id="new-comment" rows="2" maxlength="2000" placeholder="Добавить запись в историю"></textarea></label>'+
    (state.user.role!=="client"?'<label class="checklist-row"><input type="checkbox" id="comment-internal"> Внутренний комментарий</label>':"")+
    '<button type="button" class="btn btn-outline" data-comment>Добавить комментарий</button></div>';
  modal("Заявка #"+short(t.id),body);
}

async function assignment(){
  const t=state.selected;const candidates=await api("/engineers/suggestions?ticket_id="+t.id);
  state.suggestions=candidates;
  modal("Назначение инженера",
    '<p class="muted">Алгоритм: компетенция 50, смена 25, район 15, загрузка до 10 баллов. Недопустимые кандидаты блокируются сервером.</p>'+
    candidates.slice(0,16).map(c=>'<div class="candidate"><div><b>'+safe(c.name)+'</b><small>'+
      safe(c.reason)+' · '+safe(c.area)+' · '+c.load+' активных задач · '+c.score+' баллов</small>'+
      '<div class="score-bar"><span style="width:'+c.score+'%"></span></div></div>'+
      '<button class="btn '+(c.eligible?"btn-primary":"btn-outline")+'" data-choose-engineer="'+safe(c.id)+'" '+(c.eligible?"":"disabled")+'>Назначить ↗</button></div>').join(""));
}
async function actModal(id){
  const a=await api("/acts/"+id);
  let c=a.content||{};
  const canReview=["client","manager"].includes(state.user.role)&&a.status==="pending";
  const body='<div>'+status(a.status)+' <span class="muted">Редакция '+a.version+'</span></div>'+
    '<div class="detail-meta"><div><small>Заявка</small><strong>#'+short(a.ticket_id)+'</strong></div>'+
    '<div><small>Начало</small><strong>'+date(c.started_at)+'</strong></div>'+
    '<div><small>Завершение</small><strong>'+date(c.finished_at)+'</strong></div></div>'+
    '<h3 class="subheading">ВЫПОЛНЕННЫЕ РАБОТЫ</h3>'+
    (c.work||[]).map(x=>'<p>'+safe(x.summary||"Запись о работах")+'</p>').join("")+
    '<h3 class="subheading">МАТЕРИАЛЫ</h3><p>'+safe((c.materials||[]).map(x=>x.name+" — "+x.quantity).join(", ")||"Не указаны")+'</p>'+
    '<p class="muted">Исполнитель: '+short(c.engineer_id)+' · Прикреплено фотографий: '+safe(c.attachment_count||0)+'</p>'+
    (a.reason?'<p style="color:#bd6556">Причина возврата: '+safe(a.reason)+'</p>':"")+
    (canReview?'<div class="field"><label>Причина возврата (обязательна при отклонении)<textarea id="reject-reason" maxlength="1000"></textarea></label></div>':"");
  const foot=canReview?'<button class="btn btn-red" data-act-decision="rejected" data-act-id="'+safe(a.id)+'" data-version="'+a.version+'">Вернуть на доработку</button>'+
    '<button class="btn btn-primary" data-act-decision="accepted" data-act-id="'+safe(a.id)+'" data-version="'+a.version+'">Принять акт ✓</button>':"";
  modal("Акт #"+short(id),body,foot);
}
function offlineNotice(){
  const offline=!navigator.onLine;
  return '<div class="intro-banner mobile-banner"><div><span class="mobile-state '+(offline?"offline":"")+'">'+
  (offline?"● Нет соединения":"● Соединение активно")+'</span>'+
  '<h2>Мобильная работа без остановок</h2><p>Карточки и черновики сохраняются на устройстве. Сервер подтверждает каждую отправку.</p>'+
  '<p class="last-sync">Последняя загрузка заданий: '+safe(state.lastCached?date(state.lastCached):"ещё не выполнялась")+'</p></div>'+
  '<button type="button" class="btn btn-primary" data-sync>↻ Синхронизировать</button></div>';
}
async function mobilePage(){
  const local=await dbAll("tasks");state.localTasks=local;
  const pending=await dbAll("queue");state.queue=pending;
  const items=local.filter(x=>x.assignee_id===state.user.id&&["assigned","accepted","in_progress","returned"].includes(x.status));
  let out=pageHead("ПОЛЕВОЙ МОДУЛЬ","Рабочее место инженера","Офлайн-карточки, чек-листы и подтверждённые сервером действия.",
    '<button class="btn btn-outline" data-sync>↻ Обновить данные</button>')+
    offlineNotice()+'<div class="stats">'+stat("Карточки на устройстве",items.length,"Сохранённые задания","▤")+
    stat("В очереди",pending.filter(x=>!x.conflict).length,"Ещё не доставлены","◷")+
    stat("Конфликты",pending.filter(x=>x.conflict).length,"Нужна проверка инженера","⚠",true)+
    stat("Соединение",navigator.onLine?"Онлайн":"Офлайн","Состояние сети устройства","◉")+'</div>';
  out+='<div class="section-divider"><h2>Мои задания</h2><span>'+items.length+' доступно локально</span></div>';
  out+='<div class="mobile-grid">'+(items.length?items.map(t=>
    '<article class="offline-card"><div class="offline-kicker">'+safe(t.priority)+' / #'+short(t.id)+'</div>'+
    '<h3>'+safe(t.site)+'</h3><p>'+safe(t.description)+'</p>'+
    '<div class="offline-meta">'+status(t.status)+' <span>'+safe(t.asset)+'</span></div>'+
    '<button type="button" class="btn btn-dark" data-work-ticket="'+safe(t.id)+'">Открыть чек-лист ↗</button></article>').join(""):
    '<div class="empty">На этом устройстве нет доступных заданий. Подключитесь к сети и обновите данные.</div>')+'</div>';
  out+=panel("Очередь синхронизации","Записи остаются на устройстве до подтверждения сервера",
    '<div class="panel-body" id="queue-content"><p class="sync-progress" id="sync-progress" role="status" aria-live="polite"></p>'+
    (pending.length?pending.map(x=>
      '<div class="queue-row"><div><b>#'+short(x.uuid)+'</b><div>'+safe(x.summary||"Запись о работах")+'</div>'+
      '<small class="'+(x.conflict?"warning":"muted")+'">'+(x.conflict?"Требует проверки: "+safe(x.conflict):"Ожидает отправки")+'</small></div>'+
      (x.conflict?'<button class="btn btn-outline" data-review-conflict="'+safe(x.uuid)+'">Разобрать ↗</button>':"")+
      '</div>').join(""):'<div class="empty empty-compact">Очередь пуста. Все действия подтверждены.</div>')+'</div>');
  return out;
}

async function workTicket(id){
  const t=(await dbAll("tasks")).find(x=>x.id===id);
  if(!t)return toast("Карточка не найдена в локальном хранилище",true);
  const required=t.required||CHECKS[t.asset_kind]||["inspection","result"];
  if(t.status==="assigned")return toast("Сначала примите заявку при наличии сети",true);
  modal("Рабочий чек-лист",
    '<div>'+priority(t.priority)+' '+status(t.status)+'</div><h3 style="margin-top:15px">'+safe(t.site)+'</h3>'+
    '<p class="muted">'+safe(t.asset)+' · версия '+t.version+'</p>'+
    '<form id="work-form" data-id="'+safe(t.id)+'" data-version="'+t.version+'"><h3 class="subheading">ПРОВЕРКИ</h3>'+
    required.map(c=>'<label class="checklist-row"><input type="checkbox" name="check" value="'+safe(c)+'"><span>'+safe(CHECK_LABEL[c]||c)+'</span></label>').join("")+
    '<div class="form-stack" style="margin-top:20px"><label>Описание выполненных работ<textarea name="summary" rows="3" maxlength="2000" required placeholder="Фактические действия и результат"></textarea></label>'+
    '<label>Использованные материалы (по одному в строке, название:количество)<textarea name="materials" rows="2" placeholder="Кабель:2"></textarea></label>'+
    '<label>Фотографии (до 5 файлов по 10 МБ)<input type="file" name="photos" accept="image/jpeg,image/png,image/webp" multiple></label>'+
    '<button type="submit" class="btn btn-primary btn-wide">Сохранить локально <span>↗</span></button></div></form>');
}
function adminPage(){
  const summary=state.adminSummary||{sites:0,assets:0,contracts:0,policies:0,engineers:0};
  const staff=state.engineers||[];
  const sites=state.sites||[];
  return pageHead("СИСТЕМНОЕ УПРАВЛЕНИЕ","Администрирование","Справочники объектов, договорные SLA, персонал и импорт данных.")+
    '<div class="stats">'+stat("Объекты",summary.sites,"Подключённые здания","▦")+
    stat("Оборудование",summary.assets,"Активы в реестре","▣")+
    stat("Договоры",summary.contracts,"Рабочие календари","◷")+
    stat("Инженеры",summary.engineers,"Профили специалистов","⬡",true)+'</div>'+
    '<div class="layout-two">'+
      panel("Импорт данных из CSV","Проверка до сохранения · ограничение 2 МиБ",
        '<div class="panel-body"><form id="admin-import-form" class="form-stack">'+
        '<label>Тип данных<select name="kind"><option value="sites">Объекты</option><option value="assets">Оборудование</option><option value="contracts">Договоры и SLA</option></select></label>'+
        '<label>CSV в UTF-8<input name="file" type="file" accept=".csv,text/csv" required></label>'+
        '<label style="display:flex;align-items:center;gap:9px"><input type="checkbox" name="dry_run" checked> Только проверить (без записи в БД)</label>'+
        '<button class="btn btn-primary" type="submit">Проверить / импортировать ↗</button></form>'+
        '<p class="muted" id="admin-import-result" style="font-size:11px;margin-top:12px">Сначала загрузите объекты, затем оборудование и договоры.</p></div>')+
      panel("Договорные политики","Новые версии не меняют старые SLA-снимки",
        '<div class="panel-body"><div class="form-stack"><label>Объект<select id="admin-site-select">'+
        sites.map(x=>'<option value="'+safe(x.id)+'" '+(x.id===state.adminSite?"selected":"")+'>'+safe(x.name)+'</option>').join("")+
        '</select></label><button class="btn btn-dark" data-edit-policy>Настроить SLA и календарь ↗</button>'+
        '<p class="muted" style="font-size:11px">Изменение договора применяется к новым обращениям. События прежних заявок остаются неизменными.</p></div></div>')+
    '</div>'+
    panel("Команда инженеров","Навыки, доступность и рабочие смены",
      '<div class="table-wrap"><table class="table"><thead><tr><th>ИНЖЕНЕР</th><th>КОМПЕТЕНЦИИ</th><th>РАЙОН</th><th>ДОСТУПНОСТЬ</th><th>НАСТРОЙКА</th></tr></thead><tbody>'+
      staff.map(x=>'<tr><td class="title-cell">'+safe(x.name)+'</td><td>'+safe(x.skills.join(", "))+'</td><td>'+safe(x.area)+'</td><td>'+
      (x.active?'<span class="status closed">Активен</span>':'<span class="status cancelled">Отключён</span>')+'</td>'+
      '<td><button class="btn btn-outline" data-edit-engineer="'+safe(x.id)+'">Редактировать ↗</button></td></tr>').join("")+
      '</tbody></table></div>')+
    panel("Административный журнал","История изменения политик, персонала и импорта",
      '<div class="panel-body"><div class="activity">'+(state.adminAudit||[]).map(a=>
      '<div class="activity-item"><span class="activity-dot">◉</span><div><b>'+safe(a.action)+' / '+safe(a.target)+'</b>'+
      '<small>'+date(a.created_at)+'</small></div></div>').join("")+
      '</div></div>');
}
async function adminPolicyModal(){
  const siteId=$("#admin-site-select")?.value||state.adminSite||state.sites[0]?.id;
  if(!siteId)return toast("Сначала добавьте объект",true);
  state.adminSite=siteId;
  const existing=await api("/admin/contracts/"+encodeURIComponent(siteId));
  const week=existing.calendar?.week||Object.fromEntries(Array.from({length:5},(_,i)=>[String(i),[["09:00","18:00"]]]));
  const policy=existing.policies?.P1||{reaction_minutes:30,resolution_minutes:240,allowed_pauses:["awaiting_access"]};
  state.adminContract=existing;
  const dayNames=["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"];
  const form='<form id="admin-policy-form" data-site="'+safe(siteId)+'" class="form-stack">'+
    '<label>Приоритет<select name="priority" id="admin-priority-select">'+["P1","P2","P3","P4"].map(x=>
      '<option value="'+x+'">'+x+'</option>').join("")+'</select></label>'+
    '<div class="form-grid"><div class="field"><label>Реакция, рабочих минут<input type="number" name="reaction_minutes" min="1" max="100800" value="'+policy.reaction_minutes+'" required></label></div>'+
    '<div class="field"><label>Решение, рабочих минут<input type="number" name="resolution_minutes" min="1" max="100800" value="'+policy.resolution_minutes+'" required></label></div></div>'+
    '<h3 class="subheading">РАБОЧИЙ КАЛЕНДАРЬ · '+safe(existing.timezone)+'</h3>'+
    dayNames.map((name,index)=>{const entries=week[String(index)]||[];
      return '<label>'+name+'<input name="intervals-'+index+'" value="'+safe(entries.map(x=>x.join("-")).join(", "))+'" placeholder="09:00-12:00, 13:00-18:00"></label>';}).join("")+
    '<label>Выходные и праздники (YYYY-MM-DD через запятую)<textarea name="holidays" rows="2">'+safe((existing.calendar?.holidays||[]).join(", "))+'</textarea></label>'+
    '<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="awaiting_access" checked> Разрешить паузу «ожидание доступа»</label>'+
    '<label style="display:flex;gap:8px;align-items:center"><input type="checkbox" name="awaiting_parts"> Разрешить паузу «ожидание деталей»</label>'+
    '<p class="muted" style="font-size:11px">Можно указать несколько интервалов через запятую, например 09:00-12:00, 13:00-18:00. Пустой день — выходной.</p>'+
    '<button class="btn btn-primary btn-wide" type="submit">Сохранить новую редакцию SLA ↗</button></form>';
  modal("Редактор политики SLA",form);
}
function adminEngineerModal(id){
  const e=(state.engineers||[]).find(x=>x.id===id);
  if(!e)return;
  const skills=["electrical","hvac","low_voltage"];
  const body='<div class="section-kicker">'+safe(e.name)+'</div><form id="admin-profile-form" data-id="'+safe(id)+'" class="form-stack">'+
    '<label>Район обслуживания<input name="area" value="'+safe(e.area)+'" required maxlength="80"></label>'+
    '<div><div class="overline">КОМПЕТЕНЦИИ</div>'+skills.map(s=>
      '<label class="checklist-row"><input name="skill" type="checkbox" value="'+s+'" '+(e.skills.includes(s)?"checked":"")+'> '+s+'</label>').join("")+'</div>'+
    '<label style="display:flex;gap:8px;align-items:center"><input name="active" type="checkbox" '+(e.active?"checked":"")+'> Активен</label>'+
    '<button class="btn btn-primary" type="submit">Сохранить профиль</button></form>'+
    '<h3 class="subheading">ДОБАВИТЬ СМЕНУ</h3>'+
    '<form id="admin-shift-form" data-id="'+safe(id)+'" class="form-stack"><label>Начало смены<input type="datetime-local" name="starts_at" required></label>'+
    '<label>Конец смены<input type="datetime-local" name="ends_at" required></label><button class="btn btn-dark" type="submit">Добавить смену ↗</button></form>'+
    '<h3 class="subheading">ПОСЛЕДНИЕ СМЕНЫ</h3>'+e.shifts.map(x=>
      '<p class="muted" style="font-size:11px">'+date(x.starts_at)+' — '+date(x.ends_at)+'</p>').join("");
  modal("Профиль инженера",body);
}
function updateNetwork(){
  const online=navigator.onLine;
  $("#network-led").classList.toggle("offline",!online);
  $("#network-label").textContent=online?"Соединение активно":"Нет соединения";
  const indicator=$("#live-state");
  if(indicator){indicator.innerHTML="<i></i> "+(online?"ONLINE":"OFFLINE");indicator.classList.toggle("offline",!online);
    indicator.setAttribute("aria-label",online?"Система подключена":"Нет подключения к сети");}
}
async function dbOpen(databaseName){
  if(!databaseName&&!state.user?.id)throw new Error("Требуется вход для локального хранилища");
  return new Promise((resolve,reject)=>{
    const request=indexedDB.open(databaseName||("FieldPulseOfflineV2-"+state.user.id),1);
    request.onupgradeneeded=()=>{const db=request.result;db.createObjectStore("tasks",{keyPath:"id"});db.createObjectStore("queue",{keyPath:"uuid"});};
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
  });
}
async function storeOp(name,mode,op){
  const db=await dbOpen();return new Promise((resolve,reject)=>{
    const tx=db.transaction(name,mode),obj=tx.objectStore(name),request=op(obj);
    let value;
    request.onsuccess=()=>{value=request.result;};
    tx.oncomplete=()=>{db.close();resolve(value);};
    tx.onabort=()=>{db.close();reject(tx.error||new Error("Локальная транзакция отклонена"));};
    tx.onerror=()=>{};
  });
}
const dbAll=name=>storeOp(name,"readonly",o=>o.getAll());
const dbPut=(name,value)=>storeOp(name,"readwrite",o=>o.put(value));
const dbDel=(name,key)=>storeOp(name,"readwrite",o=>o.delete(key));
async function migrateLegacy(){
  // Preserve offline work by migrating only records associated with the signed-in engineer.
  if(state.user?.role!=="engineer"||typeof indexedDB.databases!=="function")return;
  if(!(await indexedDB.databases()).some(x=>x.name==="FieldPulseOfflineV1"))return;
  const old=await dbOpen("FieldPulseOfflineV1");
  function readOld(name,mode,fn){
    return new Promise((resolve,reject)=>{
      const tx=old.transaction(name,mode),req=fn(tx.objectStore(name));
      let value;
      req.onsuccess=()=>{value=req.result;};
      tx.oncomplete=()=>resolve(value);
      tx.onabort=()=>reject(tx.error||new Error("Ошибка переноса старого черновика"));
    });
  }
  try{
    const tasks=await readOld("tasks","readonly",o=>o.getAll());
    const mine=tasks.filter(t=>t.assignee_id===state.user.id);
    const authorized=new Set(mine.map(t=>t.id));
    const queue=(await readOld("queue","readonly",o=>o.getAll())).filter(x=>authorized.has(x.ticket_id));
    for(const t of mine)await dbPut("tasks",t);
    for(const x of queue)await dbPut("queue",x);
    for(const t of mine)await readOld("tasks","readwrite",o=>o.delete(t.id));
    for(const x of queue)await readOld("queue","readwrite",o=>o.delete(x.uuid));
  }finally{old.close();}
}
async function cacheTasks(){
  if(!navigator.onLine)return;
  const rows=await api("/tickets?limit=500");
  const allowed=new Set(rows.filter(t=>t.assignee_id===state.user.id).map(t=>t.id));
  for(const saved of await dbAll("tasks"))if(!allowed.has(saved.id))await dbDel("tasks",saved.id);
  for(const t of rows)if(t.assignee_id===state.user.id){
    try{t.required=(await api("/tickets/"+t.id+"/checklist")).required;}catch{t.required=CHECKS[t.asset_kind]||[];}
    await dbPut("tasks",t);
  }
  state.lastCached=new Date().toISOString();
  try{localStorage.setItem("FieldPulseLastCached:"+state.user.id,state.lastCached);}catch{}
}
async function sync(){
  if(!navigator.onLine){toast("Нет соединения. Черновики сохранены локально.",true);return;}
  try{
    if(state.user.role==="engineer"){
      await cacheTasks();
      const queue=await dbAll("queue");
      const pending=queue.filter(x=>!x.conflict).sort((a,b)=>String(a.client_at).localeCompare(String(b.client_at)));
      if(pending.length){
        let applied=0;
        for(let offset=0;offset<pending.length;offset+=30){
          const progress=$("#sync-progress");
          if(progress)progress.textContent="Передача действий: "+(offset+1)+"–"+Math.min(pending.length,offset+30)+" из "+pending.length;
          const batch=pending.slice(offset,offset+30);
          const result=await api("/mobile/sync",{method:"POST",body:{actions:batch.map(({conflict,server_version,server_status,...x})=>x)}});
          for(const r of result.results){
            const item=batch.find(x=>x.uuid===r.uuid);if(!item)continue;
            if(["applied","duplicate"].includes(r.status)){
              await dbDel("queue",item.uuid);applied+=1;
            }else{
              item.conflict=r.reason||r.status;item.server_version=r.server_version;
              item.server_status=r.server_status;await dbPut("queue",item);
            }
          }
        }
        toast("Синхронизация завершена. Подтверждено: "+applied+" из "+pending.length);
      }else toast(queue.length?"Есть неразобранные конфликты":"Данные задач актуальны");
      state.tickets=await api("/tickets?limit=500");
      if(state.page==="mobile")$("#content").innerHTML=await mobilePage();
    }else {await refresh();}
  }catch(err){toast("Ошибка синхронизации: "+err.message,true);}
}
async function conflictModal(id){
  const item=(await dbAll("queue")).find(x=>x.uuid===id);if(!item)return;
  modal("Конфликт синхронизации",
    '<p>Сервер отклонил операцию <strong>'+short(item.uuid)+'</strong>. Локальная запись сохранена.</p>'+
    '<div class="detail-meta"><div><small>Локальная версия</small><strong>'+item.base_version+'</strong></div>'+
    '<div><small>Версия сервера</small><strong>'+safe(item.server_version||"нет доступа")+'</strong></div>'+
    '<div><small>Причина</small><strong>'+safe(item.conflict)+'</strong></div></div>'+
    '<p class="muted">Сначала проверьте статус заявки и историю изменений. Повторную отправку отменённого задания выполнять нельзя.</p>'+
    '<p>Ваша запись: '+safe(item.summary)+'</p>',
    '<button class="btn btn-outline" data-close>Оставить в очереди</button>'+
    '<button class="btn btn-red" data-delete-conflict="'+safe(item.uuid)+'">Удалить черновик</button>');
}
async function fileBase64(file){
  return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(",")[1]);reader.onerror=()=>reject(reader.error);reader.readAsDataURL(file);});
}
async function refresh(){
  try{
    const role=state.user.role;
    const results=await Promise.all([api("/tickets?limit=500"),api("/sites")]);
    state.tickets=results[0];state.sites=results[1];
    if(["admin","dispatcher","manager"].includes(role)){
      state.report=await api("/reports/overview");
      if(state.page==="inbox")state.notifications=await api("/notifications");
      if(state.page==="reports"){
        [state.siteReport,state.operations]=await Promise.all([
          api("/reports/sites"),api("/reports/operations")]);
      }
    }
    if(state.page==="acts")state.acts=await api("/acts");
    if(state.page==="admin" && role==="admin"){
      [state.adminSummary,state.engineers,state.adminAudit]=await Promise.all([
        api("/admin/summary"),api("/admin/engineers"),api("/admin/audit?limit=12")]);
    }
    if(role==="engineer")await cacheTasks();
  }catch(err){
    if(state.page!=="mobile"&&!navigator.onLine){toast("Нет сети. Перейдите в мобильное рабочее место.",true);}
    else toast(err.message,true);
  }
  render();
}
async function setPage(page){
  if(!navItems().includes(page))page="overview";
  const content=$("#content");
  if(content&&!content.children.length){
    content.innerHTML='<div class="page-loading" role="status" aria-live="polite"><span class="section-kicker">ЗАГРУЗКА РАБОЧЕГО РАЗДЕЛА</span>'+
      '<div class="skeleton skeleton-title"></div><div class="skeleton skeleton-subtitle"></div>'+
      '<div class="skeleton-grid"><div class="skeleton skeleton-card"></div><div class="skeleton skeleton-card"></div>'+
      '<div class="skeleton skeleton-card"></div></div></div>';
  }
  state.page=page;state.filter="";
  setIdentity();
  if(page==="mobile"){try{$("#content").innerHTML=await mobilePage();}catch(err){toast(err.message,true);}}
  else {await refresh();}
  if(window.innerWidth<=800)closeSidebar();
}
function render(){
  setIdentity();const el=$("#content");
  if(state.page==="overview")el.innerHTML=overview();
  if(state.page==="tickets")el.innerHTML=ticketsPage();
  if(state.page==="sites")el.innerHTML=sitesPage();
  if(state.page==="acts")el.innerHTML=actsPage();
  if(state.page==="reports")el.innerHTML=reportsPage();
  if(state.page==="inbox")el.innerHTML=inboxPage();
  if(state.page==="admin")el.innerHTML=adminPage();
}
async function connectEvents(){
  if(state.eventStream)state.eventStream.close();
  if(!navigator.onLine||state.user.role==="client")return;
  const stream=new EventSource("/api/v1/board/events");
  state.eventStream=stream;
  let last=0;
  stream.addEventListener("board",()=>{const time=Date.now();if(time-last<1100)return;last=time;
    if(state.page!=="mobile")refresh().catch(()=>{});});
}
async function loginSubmit(form){
  const obj=Object.fromEntries(new FormData(form));
  const user=await api("/auth/login",{method:"POST",body:obj});
  state.user=user;sessionStorage.setItem("fieldpulse-current-user",JSON.stringify(user));
  try{state.lastCached=localStorage.getItem("FieldPulseLastCached:"+user.id)||null;}catch{}
  state.page=user.role==="engineer"?"mobile":"overview";
  viewAuth();if(user.role==="engineer")await migrateLegacy();
  await setPage(state.page);connectEvents();
  if(user.role==="engineer")await sync();
}
async function logout(){
  try{await api("/auth/logout",{method:"POST"});}catch{}
  if(state.eventStream)state.eventStream.close();
  state.user=null;sessionStorage.removeItem("fieldpulse-current-user");
  closeModal();viewAuth();
}
async function init(){
  updateNetwork();
  if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js").catch(()=>{});
  try{state.user=await api("/auth/me");sessionStorage.setItem("fieldpulse-current-user",JSON.stringify(state.user));}
  catch{if(!navigator.onLine){try{state.user=JSON.parse(sessionStorage.getItem("fieldpulse-current-user")||"null");}catch{}}}
  viewAuth();
  if(state.user){
    try{state.lastCached=localStorage.getItem("FieldPulseLastCached:"+state.user.id)||null;}catch{}
    state.page=state.user.role==="engineer"?"mobile":"overview";
    if(state.user.role==="engineer")await migrateLegacy();
    await setPage(state.page);connectEvents();}
}
document.addEventListener("click",async event=>{
  const el=event.target.closest("button,[data-ticket]");
  if(!el)return;
  try{
    if(el.matches("[data-page]")){await setPage(el.dataset.page);return;}
    if(el.matches("[data-theme]")){
      ui.theme=el.dataset.theme;savePrefs();applyAppearance();return;
    }
    if(el.matches("[data-density]")){
      ui.density=el.dataset.density;savePrefs();applyAppearance();return;
    }
    if(el.matches("[data-ticket-page]")){
      state.ticketPage=Number(el.dataset.ticketPage);updateTicketResults();return;
    }
    if(el.matches("[data-close]")){closeModal();return;}
    if(el.matches("[data-create-ticket]")){await createTicketModal();return;}
    if(el.matches("[data-edit-policy]")){await adminPolicyModal();return;}
    if(el.matches("[data-edit-engineer]")){adminEngineerModal(el.dataset.editEngineer);return;}
    if(el.matches("[data-demo]")){$("#login-form [name=email]").value=el.dataset.demo;$("#login-form [name=password]").value="FieldPulseDemo!2026";return;}
    if(el.matches("[data-ticket]")){await details(el.dataset.ticket);return;}
    if(el.matches("[data-act]")){await actModal(el.dataset.act);return;}
    if(el.matches("[data-sync]")){await sync();return;}
    if(el.matches("[data-work-ticket]")){await workTicket(el.dataset.workTicket);return;}
    if(el.matches("[data-review-conflict]")){await conflictModal(el.dataset.reviewConflict);return;}
    if(el.matches("[data-delete-conflict]")){
      if(confirm("Удалить локальный черновик? Это действие нельзя отменить.")){
        await dbDel("queue",el.dataset.deleteConflict);closeModal();$("#content").innerHTML=await mobilePage();
      }return;
    }
    if(el.matches("[data-task-action]")){
      const action=el.dataset.taskAction,t=state.selected;
      if(action==="assign"){await assignment();return;}
      if(action==="reconcile"){
        const result=await api("/reports/tickets/"+t.id+"/reconcile");
        modal("Аудит SLA #"+short(t.id),'<div class="detail-meta"><div><small>Рабочие минуты реакции</small><strong>'+safe(result.reaction_minutes??"—")+'</strong></div>'+
          '<div><small>Рабочие минуты паузы</small><strong>'+safe(result.approved_pause_minutes)+'</strong></div>'+
          '<div><small>Нарушение</small><strong>'+safe(result.recorded_violations.join(", ")||"Не зарегистрировано")+'</strong></div></div>'+
          '<h3 class="subheading">ПОДТВЕРЖДЁННЫЕ ПАУЗЫ</h3>'+
          (result.pauses.map(p=>'<p>'+safe(p.reason)+': '+date(p.started_at)+' — '+date(p.ended_at)+' ('+safe(p.working_minutes)+' мин)</p>').join("")||'<p class="muted">Пауз не было</p>')+
          '<h3 class="subheading">ФАКТЫ</h3>'+result.timeline.map(x=>'<p><b>'+safe(x.type)+'</b> — '+date(x.at)+'</p>').join(""));
        return;
      }
      let body={version:t.version};
      if(action==="pause"){const reason=prompt("Причина паузы (awaiting_access / awaiting_parts)","awaiting_access");if(!reason)return;body.reason=reason;}
      if(action==="cancel"&&!confirm("Отменить заявку?"))return;
      await api("/tickets/"+t.id+"/"+action,{method:"POST",body});toast("Изменения сохранены");
      await refresh();await details(t.id);return;
    }
    if(el.matches("[data-choose-engineer]")){
      const t=state.selected;
      await api("/tickets/"+t.id+"/assign",{method:"POST",body:{version:t.version,engineer_id:el.dataset.chooseEngineer}});
      toast("Инженер назначен");await refresh();await details(t.id);return;
    }
    if(el.matches("[data-comment]")){
      const t=state.selected,txt=$("#new-comment").value;
      if(!txt.trim())return;
      await api("/tickets/"+t.id+"/comments",{method:"POST",body:{text:txt,internal:!!$("#comment-internal")?.checked}});
      await details(t.id);return;
    }
    if(el.matches("[data-act-decision]")){
      const reason=$("#reject-reason")?.value||"";
      if(el.dataset.actDecision==="rejected"&&!reason.trim())return toast("Укажите причину возврата",true);
      await api("/acts/"+el.dataset.actId+"/decision",{method:"POST",body:{version:Number(el.dataset.version),decision:el.dataset.actDecision,reason}});
      closeModal();toast("Решение по акту сохранено");await refresh();return;
    }
  }catch(err){toast(err.message,true);}
});
document.addEventListener("submit",async event=>{
  const form=event.target;
  if(!["login-form","create-ticket-form","work-form","admin-import-form","admin-policy-form","admin-profile-form","admin-shift-form"].includes(form.id))return;
  event.preventDefault();
  try{
    if(form.id==="login-form"){await loginSubmit(form);return;}
    if(form.id==="admin-import-form"){
      const fd=new FormData();fd.append("file",form.elements.file.files[0]);
      const kind=form.elements.kind.value;
      const dry=form.elements.dry_run.checked;
      const result=await api("/admin/import/"+kind+"?dry_run="+dry,{method:"POST",body:fd});
      const message=(dry?"Проверено, без изменений: ":"Импортировано: ")+result.created_or_updated;
      toast(message);await refresh();$("#admin-import-result").textContent=message;
      return;
    }
    if(form.id==="admin-policy-form"){
      const fd=new FormData(form),week={};
      const valid=/^([01]\d|2[0-3]):[0-5]\d$/;
      for(let i=0;i<7;i++){
        const raw=String(fd.get("intervals-"+i)||"").trim();
        if(!raw)continue;
        week[String(i)]=raw.split(",").map(part=>{
          const pair=part.trim().split("-").map(s=>s.trim());
          if(pair.length!==2||!pair.every(x=>valid.test(x)))throw new Error("Некорректный интервал для дня "+(i+1));
          return pair;
        });
      }
      const holidays=String(fd.get("holidays")||"").split(",").map(x=>x.trim()).filter(Boolean);
      const payload={reaction_minutes:Number(fd.get("reaction_minutes")),resolution_minutes:Number(fd.get("resolution_minutes")),
        week,holidays,allowed_pauses:["awaiting_access","awaiting_parts"].filter(x=>fd.has(x))};
      await api("/admin/contracts/"+form.dataset.site+"/policies/"+fd.get("priority"),{method:"PUT",body:payload});
      closeModal();toast("Новая редакция политики SLA создана");await refresh();return;
    }
    if(form.id==="admin-profile-form"){
      const fd=new FormData(form);
      await api("/admin/engineers/"+form.dataset.id+"/profile",{method:"PUT",
        body:{skills:fd.getAll("skill"),area:String(fd.get("area")),active:fd.has("active")}});
      closeModal();toast("Профиль обновлён");await refresh();return;
    }
    if(form.id==="admin-shift-form"){
      const fd=new FormData(form);
      const starts_at=new Date(String(fd.get("starts_at"))).toISOString();
      const ends_at=new Date(String(fd.get("ends_at"))).toISOString();
      await api("/admin/engineers/"+form.dataset.id+"/shifts",{method:"POST",body:{starts_at,ends_at}});
      closeModal();toast("Смена сохранена");await refresh();return;
    }
    if(form.id==="create-ticket-form"){
      const data=Object.fromEntries(new FormData(form));const photo=data.photo;delete data.photo;
      const t=await api("/tickets",{method:"POST",headers:{"Idempotency-Key":crypto.randomUUID()},body:data});
      if(photo&&photo.size){const fd=new FormData();fd.append("file",photo);
        try{await api("/tickets/"+t.id+"/attachments",{method:"POST",body:fd});}
        catch(err){toast("Заявка создана, но фото не загружено: "+err.message,true);}}
      closeModal();await refresh();await details(t.id);toast("Заявка зарегистрирована");return;
    }
    if(form.id==="work-form"){
      const data=new FormData(form);const raw=String(data.get("materials")||"").split("\n").map(x=>x.trim()).filter(Boolean);
      const materials=raw.map(line=>{const parts=line.split(":");return {name:parts[0].trim(),quantity:Number(parts[1]||1)};});
      if(materials.some(x=>!x.name||!Number.isFinite(x.quantity)||x.quantity<0))throw new Error("Проверьте количество материалов");
      const files=Array.from($('input[name="photos"]',form).files);
      if(files.length>5||files.some(f=>f.size>10*1024*1024))throw new Error("Не более 5 фотографий по 10 МБ");
      const photos=await Promise.all(files.map(async f=>({name:f.name,data:await fileBase64(f)})));
      const checks={};for(const x of data.getAll("check"))checks[x]=true;
      const action={uuid:crypto.randomUUID(),ticket_id:form.dataset.id,base_version:Number(form.dataset.version),
        client_at:new Date().toISOString(),summary:String(data.get("summary")||""),checklist:checks,materials,photos};
      await dbPut("queue",action);closeModal();toast("Черновик сохранён на устройстве");
      if(navigator.onLine)await sync();else $("#content").innerHTML=await mobilePage();
    }
  }catch(err){$("#login-error").textContent=form.id==="login-form"?err.message:"";toast(err.message,true);}
});
document.addEventListener("change",e=>{
  if(e.target.id==="admin-priority-select"){
    const policy=state.adminContract?.policies?.[e.target.value];
    if(policy){
      $('input[name="reaction_minutes"]',e.target.form).value=policy.reaction_minutes;
      $('input[name="resolution_minutes"]',e.target.form).value=policy.resolution_minutes;
      ['awaiting_access','awaiting_parts'].forEach(k=>$('input[name="'+k+'"]',e.target.form).checked=policy.allowed_pauses.includes(k));
    }
  }
  if(e.target.id==="admin-site-select")state.adminSite=e.target.value;
  if(e.target.id==="new-site")loadAssets();
  if(e.target.id==="ticket-status-filter"){state.ticketStatus=e.target.value;state.ticketPage=1;updateTicketResults();}
  if(e.target.id==="ticket-priority-filter"){state.ticketPriority=e.target.value;state.ticketPage=1;updateTicketResults();}
  if(e.target.id==="ticket-sort"){state.ticketSort=e.target.value;state.ticketPage=1;updateTicketResults();}
});
document.addEventListener("input",e=>{
  if(e.target.id==="ticket-search"){state.filter=e.target.value;state.ticketPage=1;updateTicketResults();}
  if(e.target.id==="site-search"){
    state.siteFilter=e.target.value;
    const siteGrid=$("#site-grid");if(siteGrid)siteGrid.innerHTML=siteCards();
    const count=$("#site-count");if(count)count.textContent=state.sites.filter(s=>[s.name,s.city,s.area,s.timezone].some(x=>String(x||"").toLocaleLowerCase("ru").includes(state.siteFilter.trim().toLocaleLowerCase("ru")))).length+" найдено";
  }
});
function closeSidebar(){
  const sidebar=$(".sidebar"),backdrop=$("#sidebar-backdrop"),menu=$("#menu-toggle");
  sidebar.classList.remove("open");backdrop.hidden=true;
  menu.setAttribute("aria-expanded","false");menu.setAttribute("aria-label","Открыть меню");
}
function toggleSidebar(){
  const sidebar=$(".sidebar"),backdrop=$("#sidebar-backdrop"),menu=$("#menu-toggle");
  const opening=!sidebar.classList.contains("open");
  sidebar.classList.toggle("open",opening);backdrop.hidden=!opening;
  menu.setAttribute("aria-expanded",String(opening));
  menu.setAttribute("aria-label",opening?"Закрыть меню":"Открыть меню");
}
$("#logout").addEventListener("click",logout);
$("#menu-toggle").addEventListener("click",toggleSidebar);
$("#sidebar-backdrop").addEventListener("click",closeSidebar);
$("#global-search").addEventListener("click",async()=>{
  if(!state.user)return;
  await setPage("tickets");$("#ticket-search")?.focus();
});
window.addEventListener("online",()=>{updateNetwork();if(state.user&&state.user.role==="engineer")sync();else if(state.user)refresh();connectEvents();});
window.addEventListener("offline",()=>{updateNetwork();if(state.eventStream)state.eventStream.close();});
document.addEventListener("keydown",e=>{
  const dialog=$("#modal-root .modal");
  if(e.key==="Escape"){
    if(dialog){e.preventDefault();closeModal();}
    else if($(".sidebar").classList.contains("open"))closeSidebar();
    return;
  }
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==="k"&&state.user){
    e.preventDefault();$("#global-search").click();return;
  }
  if(e.key==="Tab"&&dialog){
    const focusable=Array.from(dialog.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])'))
      .filter(el=>el.getClientRects().length>0);
    if(!focusable.length)return;
    const first=focusable[0],last=focusable[focusable.length-1];
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
    else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
  }
});
applyAppearance();
init().catch(err=>toast(err.message,true));
