/* FieldPulse browser client. Server authorization is authoritative; UI is only convenience. */
"use strict";
const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => Array.from(scope.querySelectorAll(selector));
const state = {user:null, page:"overview", tickets:[], sites:[], acts:[], report:null, selected:null, timeline:[], suggestions:[], filter:"", eventStream:null, localTasks:[]};
const ROLE = {admin:"Администратор",manager:"Руководитель",dispatcher:"Диспетчер",engineer:"Инженер",client:"Клиент"};
const STATUS = {new:"Новая",triage:"Триаж",assigned:"Назначена",accepted:"Принята",in_progress:"В работе",approval:"Согласование",returned:"Возврат",closed:"Закрыта",cancelled:"Отменена"};
const ICON = {overview:"◈",tickets:"▤",sites:"▦",mobile:"⬡",acts:"▣",reports:"◫",inbox:"◉"};
const TITLE = {overview:"Обзор",tickets:"Заявки",sites:"Объекты",mobile:"Мобильное рабочее место",acts:"Акты выполненных работ",reports:"Аналитика",inbox:"Уведомления"};
const CHECKS = {electrical:["isolation","voltage","result"],hvac:["filter","pressure","result"],low_voltage:["connection","signal","result"]};
const CHECK_LABEL = {isolation:"Безопасное отключение",voltage:"Проверка напряжения",result:"Проверка результата",filter:"Состояние фильтра",pressure:"Давление системы",connection:"Подключение",signal:"Проверка сигнала",inspection:"Осмотр"};
const safe = value => String(value == null ? "" : value).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const short = id => safe(String(id || "").slice(0,8).toUpperCase());
const date = (v,zone) => {if(!v) return "—";try{return new Intl.DateTimeFormat("ru-RU",{dateStyle:"short",timeStyle:"short",timeZone:zone||"Europe/Moscow"}).format(new Date(v));}catch{return "—";}};
const status = s => '<span class="status '+safe(s)+'">'+safe(STATUS[s]||s)+'</span>';
const priority = p => '<span class="priority '+safe((p||"").toLowerCase())+'">● '+safe(p)+'</span>';
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
  return ["overview","tickets","sites","reports","acts","inbox"];
}
function setIdentity(){
  $("#user-name").textContent=state.user.name;$("#user-role").textContent=ROLE[state.user.role]||state.user.role;
  $("#user-avatar").textContent=state.user.name.split(" ").map(v=>v[0]).slice(0,2).join("").toUpperCase();
  $("#today-label").textContent=new Intl.DateTimeFormat("ru-RU",{day:"numeric",month:"long",year:"numeric"}).format(new Date());
  $("#navigation").innerHTML=navItems().map(p=>'<button class="nav-item '+(p===state.page?"active":"")+'" data-page="'+p+'"><span class="nav-icon">'+ICON[p]+'</span>'+TITLE[p]+'</button>').join("");
  $("#breadcrumb-current").textContent=TITLE[state.page];
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
function table(items){
  if(!items.length)return '<div class="empty"><div class="empty-icon">◇</div>Пока нет заявок для отображения</div>';
  return '<div class="table-wrap"><table class="table"><thead><tr><th>ЗАЯВКА</th><th>ОБЪЕКТ / ОБОРУДОВАНИЕ</th><th>ПРИОРИТЕТ</th><th>СТАТУС</th><th>ИСПОЛНИТЕЛЬ</th><th>СРОК SLA</th></tr></thead><tbody>'+
    items.map(t=>'<tr><td class="clickable" data-ticket="'+safe(t.id)+'"><div class="title-cell">#'+short(t.id)+'</div><div class="id-cell">'+date(t.created_at,t.timezone)+'</div></td>'+
    '<td><div class="title-cell">'+safe(t.site)+'</div><small class="muted">'+safe(t.asset)+'</small></td><td>'+priority(t.priority)+'</td><td>'+status(t.status)+'</td>'+
    '<td>'+safe(t.assignee||"Не назначен")+'</td><td>'+date(t.resolution_deadline,t.timezone)+'<div class="id-cell">'+safe(t.timezone)+'</div></td></tr>').join("")+'</tbody></table></div>';
}
function overview(){
  const r=state.report;const a=state.tickets;const mine=state.user.role==="engineer";const count=a.filter(t=>!["closed","cancelled"].includes(t.status)).length;
  const headline=state.user.role==="client"?"Контроль обслуживания":mine?"Мои выездные задачи":"Весь сервис на одной панели";
  const greeting=pageHead("ОПЕРАЦИОННЫЙ ОБЗОР",headline,"Актуальные заявки, сроки и рабочие процессы в режиме реального времени.",
      state.user.role==="engineer"?'<button class="btn btn-primary" data-page="mobile">↗ Перейти к задачам</button>':
      '<button class="btn btn-primary" data-create-ticket>＋ Новая заявка</button>');
  const cards='<div class="stats">'+stat("Всего заявок",r?r.total:a.length,"Зарегистрировано в системе","▣")+
    stat("В работе",r?r.open:count,"Требуют внимания","◷")+
    stat("Закрыто",r?r.closed:a.filter(x=>x.status==="closed").length,"Завершённые обращения","✓")+
    stat(r?"Нарушения SLA":"Ожидают назначения",r?r.breached_percent+"%":a.filter(x=>!x.assignee_id).length,
         r?"Процент заявок с нарушением":"Не распределены по исполнителям","↗",true)+'</div>';
  const recent=panel("Последние обращения","Недавние изменения в заявках",table(a.slice(0,7)),
                   '<button class="btn btn-outline" data-page="tickets">Все заявки ↗</button>');
  const statuses=Object.keys(STATUS).filter(x=>["new","triage","assigned","accepted","in_progress","approval","closed"].includes(x));
  const total=Math.max(1,a.length);
  const activity='<div class="panel-body"><div class="activity">'+statuses.map(k=>{
    const n=a.filter(x=>x.status===k).length;
    return '<div class="activity-item"><span class="activity-dot">◦</span><div style="flex:1"><b>'+STATUS[k]+' <span style="float:right">'+n+'</span></b>'+
      '<div class="progress-meter"><span style="width:'+Math.round(n/total*100)+'%"></span></div></div></div>';
  }).join("")+'</div></div>';
  return greeting+cards+'<div class="intro-banner"><div><h2>Работа без остановок</h2><p>Проверяйте историю, версии документов и рабочие сроки для каждой заявки.</p></div>'+
    '<button class="btn btn-primary" data-page="'+(mine?"mobile":"tickets")+'">Открыть рабочий список ↗</button></div>'+
    '<div class="layout-two">'+recent+panel("Распределение заявок","Текущая структура по статусам",activity)+'</div>';
}
function ticketsPage(){
  let a=state.tickets.filter(t=>!state.filter||(t.site+" "+t.description+" "+t.id+" "+t.asset).toLowerCase().includes(state.filter.toLowerCase()));
  return pageHead("УПРАВЛЕНИЕ ЗАЯВКАМИ","Реестр обращений","Все обращения с историей, дедлайнами и ответственными.",
      state.user.role==="engineer"?'': '<button class="btn btn-primary" data-create-ticket>＋ Создать заявку</button>')+
    '<div class="panel"><div class="filters"><input class="search" id="ticket-search" type="search" placeholder="Поиск по объекту, номеру, описанию" value="'+safe(state.filter)+'">'+
    '<select id="ticket-status-filter"><option value="">Все статусы</option>'+Object.keys(STATUS).map(s=>'<option value="'+s+'">'+STATUS[s]+'</option>').join("")+'</select>'+
    '<span class="muted">'+a.length+' обращений</span></div><div id="ticket-table">'+table(a)+'</div></div>';
}
function sitesPage(){
  return pageHead("ОБЪЕКТЫ И ОБОРУДОВАНИЕ","Обслуживаемые объекты","Объекты, доступные вашей роли, с указанием района и часового пояса.")+
    '<div class="site-grid">'+state.sites.slice(0,60).map(s=>
    '<div class="site-card"><div class="site-card-icon">▧</div><h3>'+safe(s.name)+'</h3><p>'+safe(s.city)+' / '+safe(s.area)+'</p>'+
    '<div class="site-card-foot"><span>'+safe(s.timezone)+'</span><span>Объект ↗</span></div></div>').join("")+'</div>';
}
function actsPage(){
  const a=state.acts;
  return pageHead("ДОКУМЕНТООБОРОТ","Акты и согласование","Каждое решение сохраняет предыдущие редакции документа.")+
    panel("История актов",a.length+" редакций",
    a.length?'<div class="table-wrap"><table class="table"><thead><tr><th>АКТ</th><th>ЗАЯВКА</th><th>РЕДАКЦИЯ</th><th>СТАТУС</th><th>ДАТА</th><th>ДЕЙСТВИЕ</th></tr></thead><tbody>'+
    a.map(x=>'<tr><td class="title-cell">#'+short(x.id)+'</td><td class="clickable" data-ticket="'+safe(x.ticket_id)+'">#'+short(x.ticket_id)+'</td><td>v'+x.version+'</td><td>'+status(x.status)+'</td><td>'+date(x.created_at)+'</td>'+
    '<td><button class="btn btn-outline" data-act="'+safe(x.id)+'">Просмотр ↗</button></td></tr>').join("")+'</tbody></table></div>':
    '<div class="empty">Акты появятся здесь после выполнения работ.</div>');
}
function reportsPage(){
  const r=state.report||{total:0,open:0,breached_percent:0,repeat_percent:0,load:[]};
  const load=r.load||[];const max=Math.max(...load.map(a=>a.count),1);
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
    ].map(x=>'<div class="activity-item"><span class="activity-dot">↗</span><div><b>'+x[0]+'</b><small>'+x[1]+'</small></div></div>').join("")+'</div></div>')+'</div>';
}
function inboxPage(){
  const notifications=state.notifications||[];
  return pageHead("МОНИТОРИНГ И ЭСКАЛАЦИИ","Центр уведомлений","Локальный mock inbox для предупреждений и нарушений SLA.")+
    panel("Журнал отправки",notifications.length+" сообщений",
    '<div class="panel-body"><div class="activity">'+(notifications.length?
      notifications.map(n=>'<div class="activity-item"><span class="activity-dot">◉</span><div><b>'+safe(n.subject)+'</b><small>'+date(n.at)+' / '+safe(n.recipient)+'</small><p style="font-size:11px">'+safe(n.body)+'</p></div></div>').join(""):
      '<div class="empty">Новые уведомления появятся по мере приближения SLA.</div>')+'</div></div>');
}
function modal(title,body,foot="",small=false){
  $("#modal-root").innerHTML='<div class="modal-overlay" id="overlay"><section class="modal '+(small?"sm":"")+'" role="dialog" aria-modal="true" aria-label="'+safe(title)+'">'+
  '<header class="modal-head"><div><div class="section-kicker">FIELD PULSE / WORKSPACE</div><h2>'+safe(title)+'</h2></div><button class="modal-close" data-close>×</button></header>'+
  '<div class="modal-content">'+body+'</div>'+(foot?'<div class="modal-foot">'+foot+'</div>':"")+'</section></div>';
}
function closeModal(){$("#modal-root").innerHTML="";}
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
  const t=await api("/tickets/"+id);state.selected=t;
  const timeline=await api("/tickets/"+id+"/timeline");state.timeline=timeline;
  const canStaff=["admin","dispatcher","manager"].includes(state.user.role);
  const isEngineer=state.user.role==="engineer";
  let buttons=[];
  function action(label,key,style="btn-outline"){buttons.push('<button class="btn '+style+'" data-task-action="'+key+'">'+label+'</button>');}
  if(canStaff&&t.status==="new")action("Взять в триаж","triage");
  if(canStaff&&["new","triage","returned"].includes(t.status))action("Назначить инженера","assign","btn-primary");
  if(isEngineer&&t.status==="assigned")action("Принять заявку","accept","btn-primary");
  if(isEngineer&&["accepted","returned"].includes(t.status))action("Начать работы","start","btn-primary");
  if(canStaff&&["assigned","accepted","in_progress"].includes(t.status))action("Пауза SLA","pause");
  if(canStaff&&["assigned","accepted","in_progress"].includes(t.status))action("Возобновить SLA","resume");
  if(canStaff&&["new","triage","assigned","accepted","in_progress","returned"].includes(t.status))action("Отменить","cancel","btn-red");
  if(isEngineer&&t.status==="in_progress")action("Оформить акт","submit","btn-primary");
  const desc='<div>'+priority(t.priority)+' '+status(t.status)+' <span class="muted">Версия '+t.version+'</span></div>'+
    '<h3 style="font-size:17px;margin:17px 0 5px">'+safe(t.description)+'</h3><p class="muted">'+safe(t.site)+' / '+safe(t.asset)+'</p>'+
    '<div class="detail-meta"><div><small>Создана</small><strong>'+date(t.created_at,t.timezone)+'</strong></div>'+
    '<div><small>Реакция до</small><strong>'+date(t.reaction_deadline,t.timezone)+'</strong></div>'+
    '<div><small>Решение до</small><strong>'+date(t.resolution_deadline,t.timezone)+'</strong></div></div>'+
    '<p class="muted" style="font-size:10px">Часовой пояс объекта: '+safe(t.timezone)+' · Политика '+safe(t.sla.policy_name)+'</p>'+
    '<div class="detail-buttons">'+buttons.join("")+'</div><h3 class="subheading">ХРОНОЛОГИЯ СОБЫТИЙ</h3>'+
    '<div class="timeline">'+timeline.map(e=>'<div class="timeline-entry"><b>'+safe(e.type.replaceAll("_"," "))+'</b>'+
      '<small>'+date(e.at,t.timezone)+'</small>'+(e.data && (e.data.text||e.data.reason)?
      '<p>'+safe(e.data.text||e.data.reason)+'</p>':"")+'</div>').join("")+'</div>'+
    '<div class="field" style="margin-top:16px"><label>Комментарий<textarea id="new-comment" rows="2" maxlength="2000" placeholder="Оставить запись в истории"></textarea></label>'+
    (state.user.role!=="client"?'<label style="display:flex;align-items:center;gap:8px"><input type="checkbox" id="comment-internal"> Внутренний комментарий</label>':"")+
    '<button class="btn btn-outline" data-comment>Добавить комментарий</button></div>';
  modal("Заявка #"+short(t.id),desc);
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
  return '<div class="intro-banner"><div><h2>Мобильная работа без сети</h2><p>Задачи сохраняются в устройстве. Черновики отправляются только после подтверждения сервера.</p></div>'+
  '<button class="btn btn-primary" data-sync>↻ Синхронизировать</button></div>';
}
async function mobilePage(){
  const local=await dbAll("tasks");state.localTasks=local;
  let pending=await dbAll("queue");state.queue=pending;
  const items=local.filter(x=>x.assignee_id===state.user.id && ["assigned","accepted","in_progress","returned"].includes(x.status));
  let out=pageHead("ПОЛЕВОЙ МОДУЛЬ","Рабочее место инженера","Заявки и чек-листы доступны после загрузки даже без подключения.",
      '<button class="btn btn-outline" data-sync>↻ Обновить данные</button>')+
    offlineNotice()+'<div class="stats">'+stat("Доступно на устройстве",items.length,"Загруженные карточки","▤")+
    stat("В очереди",pending.filter(x=>!x.conflict).length,"Не подтверждены сервером","◷")+
    stat("Конфликты",pending.filter(x=>x.conflict).length,"Требуют решения","⚠",true)+
    stat("Сеть",navigator.onLine?"Онлайн":"Офлайн","Локальное сохранение работает","◉")+'</div>';
  out+='<div class="mobile-grid">'+items.map(t=>'<div class="offline-card"><div class="offline-kicker">'+safe(t.priority)+' / #'+short(t.id)+'</div>'+
    '<h3>'+safe(t.site)+'</h3><p>'+safe(t.description)+'</p><div>'+status(t.status)+'</div>'+
    '<button class="btn btn-dark" data-work-ticket="'+safe(t.id)+'">Открыть чек-лист ↗</button></div>').join("")+'</div>';
  out+=panel("Очередь синхронизации","Локальные UUID не считаются серверными записями",
    '<div class="panel-body" id="queue-content">'+(pending.length?pending.map(x=>
      '<div class="queue-row"><div><b>'+short(x.uuid)+'</b><div>'+safe(x.summary||"Запись о работах")+'</div>'+
      '<small class="'+(x.conflict?"warning":"muted")+'">'+safe(x.conflict||"Ожидает доставки")+'</small></div>'+
      (x.conflict?'<button class="btn btn-outline" data-review-conflict="'+safe(x.uuid)+'">Разобрать</button>':"")+'</div>').join(""):'<div class="empty">Очередь пуста.</div>')+'</div>');
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
function updateNetwork(){
  $("#network-led").classList.toggle("offline",!navigator.onLine);
  $("#network-label").textContent=navigator.onLine?"Соединение активно":"Нет соединения";
}
async function dbOpen(){
  return new Promise((resolve,reject)=>{
    const request=indexedDB.open("FieldPulseOfflineV1",1);
    request.onupgradeneeded=()=>{const db=request.result;db.createObjectStore("tasks",{keyPath:"id"});db.createObjectStore("queue",{keyPath:"uuid"});};
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
  });
}
async function storeOp(name,mode,op){
  const db=await dbOpen();return new Promise((resolve,reject)=>{
    const tx=db.transaction(name,mode),obj=tx.objectStore(name),request=op(obj);
    request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error);
    tx.oncomplete=()=>db.close();
  });
}
const dbAll=name=>storeOp(name,"readonly",o=>o.getAll());
const dbPut=(name,value)=>storeOp(name,"readwrite",o=>o.put(value));
const dbDel=(name,key)=>storeOp(name,"readwrite",o=>o.delete(key));
async function cacheTasks(){
  if(!navigator.onLine)return;
  const rows=await api("/tickets?limit=500");
  for(const t of rows)if(t.assignee_id===state.user.id){
    try{t.required=(await api("/tickets/"+t.id+"/checklist")).required;}catch{t.required=CHECKS[t.asset_kind]||[];}
    await dbPut("tasks",t);
  }
}
async function sync(){
  if(!navigator.onLine){toast("Нет соединения. Черновики сохранены локально.",true);return;}
  try{
    if(state.user.role==="engineer"){
      await cacheTasks();
      const queue=await dbAll("queue");
      if(queue.length){
        const result=await api("/mobile/sync",{method:"POST",body:{actions:queue.filter(x=>!x.conflict).map(({conflict,...x})=>x)}});
        for(const r of result.results){
          const item=queue.find(x=>x.uuid===r.uuid);if(!item)continue;
          if(["applied","duplicate"].includes(r.status))await dbDel("queue",item.uuid);
          else {item.conflict=r.reason||r.status;item.server_version=r.server_version;item.server_status=r.server_status;await dbPut("queue",item);}
        }
        toast("Синхронизация завершена. Принято: "+result.results.filter(x=>["applied","duplicate"].includes(x.status)).length);
      }else toast("Данные задач актуальны");
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
    }
    if(state.page==="acts")state.acts=await api("/acts");
    if(role==="engineer")await cacheTasks();
  }catch(err){
    if(state.page!=="mobile"&&!navigator.onLine){toast("Нет сети. Перейдите в мобильное рабочее место.",true);}
    else toast(err.message,true);
  }
  render();
}
async function setPage(page){
  if(!navItems().includes(page))page="overview";
  state.page=page;state.filter="";
  setIdentity();
  if(page==="mobile"){try{$("#content").innerHTML=await mobilePage();}catch(err){toast(err.message,true);}}
  else {await refresh();}
  if(window.innerWidth<800)$(".sidebar").classList.remove("open");
}
function render(){
  setIdentity();const el=$("#content");
  if(state.page==="overview")el.innerHTML=overview();
  if(state.page==="tickets")el.innerHTML=ticketsPage();
  if(state.page==="sites")el.innerHTML=sitesPage();
  if(state.page==="acts")el.innerHTML=actsPage();
  if(state.page==="reports")el.innerHTML=reportsPage();
  if(state.page==="inbox")el.innerHTML=inboxPage();
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
  state.page=user.role==="engineer"?"mobile":"overview";
  viewAuth();await setPage(state.page);connectEvents();
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
  if("serviceWorker" in navigator)navigator.serviceWorker.register("/static/sw.js").catch(()=>{});
  try{state.user=await api("/auth/me");sessionStorage.setItem("fieldpulse-current-user",JSON.stringify(state.user));}
  catch{if(!navigator.onLine){try{state.user=JSON.parse(sessionStorage.getItem("fieldpulse-current-user")||"null");}catch{}}}
  viewAuth();
  if(state.user){state.page=state.user.role==="engineer"?"mobile":"overview";await setPage(state.page);connectEvents();}
}
document.addEventListener("click",async event=>{
  const el=event.target.closest("button,[data-ticket]");
  if(!el)return;
  try{
    if(el.matches("[data-page]")){await setPage(el.dataset.page);return;}
    if(el.matches("[data-close]")){closeModal();return;}
    if(el.matches("[data-create-ticket]")){await createTicketModal();return;}
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
  if(!["login-form","create-ticket-form","work-form"].includes(form.id))return;
  event.preventDefault();
  try{
    if(form.id==="login-form"){await loginSubmit(form);return;}
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
document.addEventListener("change",e=>{if(e.target.id==="new-site")loadAssets();if(e.target.id==="ticket-status-filter"){
  const value=e.target.value;$("#ticket-table").innerHTML=table(state.tickets.filter(t=>(!state.filter||JSON.stringify(t).toLowerCase().includes(state.filter.toLowerCase()))&&(!value||t.status===value)));}});
document.addEventListener("input",e=>{if(e.target.id==="ticket-search"){
  state.filter=e.target.value;const selected=$("#ticket-status-filter")?.value;
  $("#ticket-table").innerHTML=table(state.tickets.filter(t=>(!state.filter||JSON.stringify(t).toLowerCase().includes(state.filter.toLowerCase()))&&(!selected||t.status===selected)));}});
$("#logout").addEventListener("click",logout);
$("#menu-toggle").addEventListener("click",()=>$(".sidebar").classList.toggle("open"));
window.addEventListener("online",()=>{updateNetwork();if(state.user&&state.user.role==="engineer")sync();else if(state.user)refresh();connectEvents();});
window.addEventListener("offline",()=>{updateNetwork();if(state.eventStream)state.eventStream.close();});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal();});
init().catch(err=>toast(err.message,true));
