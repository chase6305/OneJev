'use strict';
const $ = id => document.getElementById(id);
const labels = {unpaid:'未付款',retry:'临时失败',processing:'处理中',paid:'付款成功',blocked:'付款被拒绝'};
const actions = {pay:'点击付款',retry:'再次尝试付款',wait:'等待当前操作',verified:'任务已完成',review:'停止，等待检查',exhausted:'已达到运行上限',error:'本次运行出错'};
const scenarios = {retry:'付款失败 → 自动重试',success:'首次付款成功',blocked:'付款被拒绝',exhausted:'持续失败 → 达到上限',pending:'一直处理中'};
const descriptions = {pay:'下一次点击将执行付款，并观察付款后的页面。',retry:'页面提示临时失败。下一步将重试一次，再次截图检查。',wait:'已有操作正在处理中，继续观察，不重复提交。',verified:'模型判断与页面的已付款状态一致。结果会保留在这里。',review:'当前情况需要检查。流程已停止，你可以回看每一步。',exhausted:'为了避免反复执行，达到尝试或观察次数上限后停止。',error:'请查看提示，排除问题后重新开始案例。'};
let session = null, selectedStep = null, readonly = false, imageData = null, activeTab = 'browser', historySignature = '', working = false;
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const pct = value => Number.isFinite(value) ? (value * 100).toFixed(1) : '—';
const ms = value => Number.isFinite(value) ? `${Math.round(value * 1000)} ms` : '—';
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let data;
  try { data = await response.json(); } catch { throw new Error('网站响应异常，请稍后重试。'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `操作失败（${response.status}）`);
  return data;
}
let toastTimer;
function toast(message) { $('toast').textContent=message;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},6000); }
function switchTab(tab) {
  activeTab=tab;
  document.querySelectorAll('.tab-panel').forEach(panel=>{panel.hidden=panel.id!==`panel-${tab}`;});
  document.querySelectorAll('[data-tab]').forEach(button=>button.classList.toggle('active',button.dataset.tab===tab));
  $('breadcrumb').textContent={browser:'浏览器任务',text:'文本判断',image:'图片理解',runs:'运行记录'}[tab];
  if(tab==='runs') loadRuns();
}
document.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>switchTab(button.dataset.tab)));
async function refreshStatus() {
  try {
    const status=await api('/api/status');
    $('model-dot').className=`status-dot ${status.state}`;
    $('model-state').textContent=status.message;
    $('model-name').textContent=status.model;
    document.body.dataset.modelState=status.state;
    renderControls();
  } catch { $('model-dot').className='status-dot error';$('model-state').textContent='网站连接中断';document.body.dataset.modelState='offline';renderControls(); }
}
$('reconnect').addEventListener('click',async()=>{try{await api('/api/connect',{});toast('正在重新连接模型…');await refreshStatus();}catch(error){toast(error.message);}});
function renderControls() {
  const busy=working||session?.busy, ready=document.body.dataset.modelState==='ready', finished=Boolean(session?.result);
  $('step').disabled=!session||busy||!ready||finished||readonly||session.auto_running;
  $('auto').disabled=!session||busy||!ready||finished||readonly;
  $('auto').hidden=Boolean(session?.auto_running);
  $('pause').hidden=!session?.auto_running;
  $('reset').disabled=working||Boolean(session?.busy);
  $('scenario').disabled=working||Boolean(session?.busy);
  $('text-submit').disabled=!ready||$('text-form').dataset.busy==='1';
  $('image-submit').disabled=!ready||$('image-form').dataset.busy==='1';
}
function applySession(data, fromHistory=false) {
  session=data;readonly=fromHistory;
  if(data.scenario) $('scenario').value=data.scenario;
  if(!fromHistory) localStorage.setItem('onejev-studio-session',data.id);
  renderSession();
}
function renderSession() {
  if(!session) return;
  const history=session.history||[];
  const observation=selectedStep===null?history.at(-1):history[selectedStep];
  const frame=observation?.image||session.frame;
  if(frame&&$('frame').getAttribute('src')!==frame) $('frame').src=frame;
  $('frame-empty').hidden=Boolean(frame);
  $('thinking').hidden=!(session.busy||working);
  const pageStatus=observation?.page_status;
  $('page-badge').textContent=labels[pageStatus]||(history.length?'等待判断':'未付款');
  $('page-badge').className=`badge ${pageStatus==='paid'?'good':['retry','blocked'].includes(pageStatus)?'bad':''}`;
  $('frame-caption').textContent=readonly?'历史案例回放 · 点击截图放大':selectedStep===null?'真实浏览器截图 · 点击放大':`回看第 ${selectedStep+1} 次观察 · 点击放大`;
  $('step-count').textContent=`${history.length} 步`;
  const completed=observation?.answers?.completed?.noul;
  $('done-value').textContent=pct(completed);
  $('done-fill').style.width=Number.isFinite(completed)?`${Math.min(100,Math.max(0,completed*100))}%`:'0';
  const statusAnswer=observation?.answers?.status;
  $('state-value').textContent=labels[statusAnswer?.choice]||'尚未判断';
  $('confidence-value').textContent=statusAnswer?`${pct(statusAnswer.probabilities?.[statusAnswer.choice])}%`:'—';
  $('time-value').textContent=ms(observation?.request_s);
  const action=observation?.decision?.action;
  $('next-action').textContent=actions[action]||'等待你的指令';
  $('action-description').textContent=descriptions[action]||'单步模式下，下一次点击才执行建议的动作。';
  $('result-badge').textContent=session.busy?'判断中':session.result==='verified'?'已完成':session.result?'已停止':session.auto_running?'自动演示':'等待下一步';
  $('result-badge').className=`badge ${session.result==='verified'?'good':session.result?'bad':''}`;
  $('done-hint').textContent=completed===undefined?'截图中的信息会成为模型的判断依据。':completed>=.85?'达到演示阈值，继续核对页面实际状态。':'尚未达到完成阈值，结合页面状态决定后续动作。';
  $('session-error').hidden=!session.error;$('session-error').textContent=session.error||'';
  $('raw-result').textContent=observation?JSON.stringify(observation,null,2):'暂无结果';
  const signature=JSON.stringify([session.id,history.length,selectedStep,session.result]);
  if(signature!==historySignature){
    historySignature=signature;
    $('timeline').innerHTML=history.length?history.map((row,index)=>`<button class="step-card ${index===(selectedStep??history.length-1)?'selected':''}" data-history-step="${index}"><img src="${esc(row.image)}" alt="第 ${index+1} 步：${esc(labels[row.page_status])}"><div><strong>${String(index+1).padStart(2,'0')} · ${esc(labels[row.page_status]||row.page_status)}</strong><small>完成概率 ${pct(row.answers?.completed?.noul)}%</small></div></button>`).join(''):'<div class="empty-line">点击「单步判断」，开始第一次观察。</div>';
    $('timeline').querySelectorAll('[data-history-step]').forEach(button=>button.addEventListener('click',()=>{selectedStep=Number(button.dataset.historyStep);renderSession();}));
  }
  renderControls();
}
async function newSession() {
  if(working) return;
  working=true;renderControls();
  try {
    if(session?.auto_running) await api(`/api/sessions/${session.id}/pause`,{});
    const data=await api('/api/sessions',{scenario:$('scenario').value});
    selectedStep=null;historySignature='';applySession(data);
  } catch(error) { toast(error.message);$('frame-empty').innerHTML='案例准备失败，请检查浏览器支持后重试。'; }
  finally { working=false;renderControls();if(session)renderSession(); }
}
$('reset').addEventListener('click',newSession);
$('scenario').addEventListener('change',newSession);
$('step').addEventListener('click',async()=>{
  if(!session||working)return;working=true;selectedStep=null;renderSession();
  try{applySession(await api(`/api/sessions/${session.id}/step`,{}));}catch(error){toast(error.message);}finally{working=false;renderSession();}
});
$('auto').addEventListener('click',async()=>{if(!session)return;selectedStep=null;try{applySession(await api(`/api/sessions/${session.id}/auto`,{delay:Number($('speed').value)}));}catch(error){toast(error.message);}});
$('pause').addEventListener('click',async()=>{try{applySession(await api(`/api/sessions/${session.id}/pause`,{}));}catch(error){toast(error.message);}});
$('speed').addEventListener('input',()=>{$('speed-label').textContent=`${Number($('speed').value)} 秒`;});
$('speed').addEventListener('change',async()=>{if(session?.auto_running)try{await api(`/api/sessions/${session.id}/auto`,{delay:Number($('speed').value)});}catch(error){toast(error.message);}});
function enlargeFrame(){const source=$('frame').src;if(!source)return;$('large-frame').src=source;$('image-dialog').showModal();}
$('frame').addEventListener('click',enlargeFrame);$('frame').addEventListener('keydown',event=>{if(event.key==='Enter')enlargeFrame();});
$('close-dialog').addEventListener('click',()=>$('image-dialog').close());
$('image-dialog').addEventListener('click',event=>{if(event.target===$('image-dialog'))$('image-dialog').close();});
const sampleTexts={refund:'我的订阅这个月被扣了两次款，请尽快帮我退还多扣的一笔，我已经等了三天了。',bug:'升级之后，每次点击保存按钮软件都会崩溃。重新安装后仍然如此，麻烦技术人员协助。',resolved:'谢谢，按照你提供的步骤操作之后已经恢复正常，问题解决了。'};
document.querySelectorAll('[data-text-sample]').forEach(button=>button.addEventListener('click',()=>{
  $('text-input').value=sampleTexts[button.dataset.textSample];$('text-preset').value=button.dataset.textSample==='resolved'?'completion':'support';$('text-statement-wrap').hidden=$('text-preset').value!=='completion';
}));
$('text-preset').addEventListener('change',()=>{$('text-statement-wrap').hidden=$('text-preset').value!=='completion';});
function answerBars(probabilities,names={}) {
  return Object.entries(probabilities).map(([name,value])=>`<div class="answer-bar"><span>${esc(names[name]||name)}</span><div class="track"><div class="fill" style="width:${Number.isFinite(value)?Math.min(100,Math.max(0,value*100)):0}%"></div></div><strong>${pct(value)}%</strong></div>`).join('');
}
function renderAnswers(target,data) {
  const names={completed:'命题成立',department:'应该分配给哪个团队？',urgent:'问题是否紧急？',frustration:'用户的着急或不满程度'};
  let content='';
  for(const [key,answer] of Object.entries(data.answers)){
    content+=`<section class="answer-block"><h3>${esc(names[key]||key)}<small>${esc(answer.type)}</small></h3>`;
    if(answer.type==='noul')content+=`<div class="answer-big">${pct(answer.noul)}<small>%</small></div>${answerBars({yes:answer.noul,no:1-answer.noul},{yes:'成立',no:'不成立'})}`;
    else if(answer.type==='choice')content+=answerBars(answer.probabilities,{billing:'账单 / 退款',technical:'技术支持',sales:'售前咨询'});
    else content+=`<div class="answer-big">${Number(answer.score).toFixed(2)}<small> / 2</small></div>${answerBars(answer.probabilities,{'0':'平静','1':'有些不满','2':'非常着急'})}`;
    content+='</section>';
  }
  content+=`<p class="answer-meta">${esc(data.model)} · 本次请求 ${ms(data.request_s)}<br>${data.usage?.input_tokens??'—'} 个输入 token · ${data.usage?.output_tokens??'—'} 个输出 token</p><details class="raw-details"><summary>查看原始结果</summary><pre>${esc(JSON.stringify(data,null,2))}</pre></details>`;
  target.innerHTML=content;
}
async function submitAsk(form,target,payload) {
  form.dataset.busy='1';renderControls();target.innerHTML='<div class="answer-empty"><span class="loader"></span><p>模型正在判断…</p></div>';
  try{renderAnswers(target,await api('/api/ask',payload));}catch(error){target.innerHTML=`<div class="inline-error">${esc(error.message)}</div>`;}
  finally{form.dataset.busy='0';renderControls();}
}
$('text-form').addEventListener('submit',event=>{event.preventDefault();submitAsk($('text-form'),$('text-answers'),{mode:'text',preset:$('text-preset').value,text:$('text-input').value,statement:$('text-statement').value});});
function loadImage(blob){
  if(!blob.type.startsWith('image/')){toast('请选择图片文件。');return;}
  if(blob.size>10*1024*1024){toast('图片超过 10 MB，请缩小后再上传。');return;}
  const reader=new FileReader();reader.onload=()=>{imageData=reader.result;$('image-preview').src=imageData;$('image-preview').hidden=false;$('upload-placeholder').hidden=true;};reader.readAsDataURL(blob);
}
$('dropzone').addEventListener('click',()=>$('image-file').click());
$('dropzone').addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();$('image-file').click();}});
$('image-file').addEventListener('change',event=>{if(event.target.files[0])loadImage(event.target.files[0]);});
['dragenter','dragover'].forEach(type=>$('dropzone').addEventListener(type,event=>{event.preventDefault();$('dropzone').classList.add('dragging');}));
['dragleave','drop'].forEach(type=>$('dropzone').addEventListener(type,event=>{event.preventDefault();$('dropzone').classList.remove('dragging');if(type==='drop'&&event.dataTransfer.files[0])loadImage(event.dataTransfer.files[0]);}));
$('image-sample').addEventListener('click',async()=>{try{const response=await fetch('/api/sample.png');if(!response.ok)throw new Error('案例图片暂时无法加载，请稍后重试。');loadImage(await response.blob());}catch(error){toast(error.message);}});
$('image-form').addEventListener('submit',event=>{event.preventDefault();if(!imageData){toast('请先上传图片，或选择案例截图。');return;}submitAsk($('image-form'),$('image-answers'),{mode:'image',image:imageData,text:$('image-context').value,statement:$('image-statement').value});});
async function loadRuns(){
  try{
    const data=await api('/api/runs');
    $('runs-list').innerHTML=data.runs.length?data.runs.map(run=>`<button class="run-row card" data-run="${esc(run.id)}"><div><h3>${esc(scenarios[run.scenario]||run.scenario)}</h3><p>${esc(new Date(run.created_at).toLocaleString('zh-CN'))}</p></div><span>${esc(actions[run.result]||(run.result?'已停止':'进行中'))}</span><span>${run.attempts??0} 次尝试</span><span class="open-run">回看过程 ↗</span></button>`).join(''):'<div class="empty-line">还没有运行记录。先去浏览器任务里试一个案例。</div>';
    $('runs-list').querySelectorAll('[data-run]').forEach(button=>button.addEventListener('click',async()=>{try{const run=await api(`/api/runs/${button.dataset.run}`);selectedStep=null;historySignature='';applySession(run,true);switchTab('browser');}catch(error){toast(error.message);}}));
  }catch(error){toast(error.message);}
}
$('refresh-runs').addEventListener('click',loadRuns);
let tick=0;
async function poll(){
  try{
    if(tick++%4===0) await refreshStatus();
    if(session&&!readonly&&!working){const data=await api(`/api/sessions/${session.id}`);applySession(data);}
  }catch(error){if(session&&!readonly){localStorage.removeItem('onejev-studio-session');session=null;renderControls();toast('案例已结束，可点击重新开始。');}}
  setTimeout(poll,750);
}
async function boot(){
  await refreshStatus();
  const saved=localStorage.getItem('onejev-studio-session');
  if(saved)try{applySession(await api(`/api/sessions/${saved}`));$('scenario').value=session.scenario;}catch{await newSession();}
  else await newSession();
  poll();
}
boot();
