'use strict';
const $ = id => document.getElementById(id);
const titles = {overview:'设备概览', settings:'参数设置', diagnostics:'语音诊断', maintenance:'服务维护'};
const phaseNames = {idle:'设备正在待机',wake:'设备已唤醒',listen:'正在聆听',think:'正在思考',answer:'正在回答',speak:'正在播报'};
const serviceNames = {voice:['语音助手','唤醒、识别、对话和播报'],ui:['设备屏幕','触屏显示与本地设置'],keys:['硬件输入','音量按键和麦克风开关'],gateway:['管理网关','为当前管理页面提供接口']};
const numericVoice = ['kws_score','kws_threshold','speech_wait_seconds','max_speech_seconds','followup_seconds','question_followup_seconds'];
const modelFields = ['chat_model','chat_provider','expert_model','tts_model','tts_voice'];
const providerNames = {bailian:'阿里云百炼',openrouter:'OpenRouter',openai:'OpenAI'};
let csrf='', page='overview', latest=null, prefsBase=null, voiceBase=null, settingsLoaded=false, polling=false, logsAt=0, loadingLogs=false, busy=false, toastTimer;
let previewController=null, previewUrl=null, previewSequence=0, modelSaving=false;
const prefsTouched=new Set();
const clock = seconds => new Date(seconds*1000).toLocaleTimeString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai'});
const mb = value => typeof value==='number' ? `${(value/1048576).toFixed(1)} MB` : '—';
const text = (id,value) => { $(id).textContent = value; };
function pill(id,label,kind='neutral'){text(id,label);$(id).className=`pill ${kind}`;}
function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').className=error?'error':'';$('toast').hidden=false;toastTimer=setTimeout(()=>$('toast').hidden=true,error?8500:5000);}
async function api(path,body,retrySession=true){
 const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),95000);
 try{
  const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:body===undefined?{}:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body),signal:controller.signal,credentials:'same-origin'});
  const data=await response.json();
  if(response.status===403&&body!==undefined&&retrySession){csrf=(await api('/api/session')).csrf;return await api(path,body,false);}
  if(!response.ok||data.ok===false)throw new Error(data.error||'操作未完成');
  return data;
 }catch(error){if(error.name==='AbortError')throw new Error('尚未确认操作结果，请刷新状态后查看');throw error;}finally{clearTimeout(timer);}
}
async function mutate(path,body,button){
 if(busy)return null;
 busy=true;const label=button?.textContent;
 if(button){button.disabled=true;button.textContent='正在处理…';}
 try{if(!csrf)csrf=(await api('/api/session')).csrf;const result=await api(path,body);toast(result.message||'操作已完成');await poll();return result;}
 catch(error){toast(error.message,true);return null;}
 finally{busy=false;if(button){button.disabled=false;button.textContent=label;}}
}
function setPage(next){
 page=titles[next]?next:'overview';location.hash=page;
 if(page!=='settings')stopPreview();
 document.querySelectorAll('[data-section]').forEach(el=>el.hidden=el.dataset.section!==page);
 document.querySelectorAll('[data-page]').forEach(el=>{el.classList.toggle('active',el.dataset.page===page);if(el.dataset.page===page)el.setAttribute('aria-current','page');else el.removeAttribute('aria-current');});
 text('page-title',titles[page]);
 if(page==='settings'&&!settingsLoaded)loadSettings();
 if(page==='diagnostics')loadLogs();
}
function levels(health){
 const lv=health?.levels||{};
 for(const kind of ['raw','aec']){
  const value=lv[kind];text(`${kind}-level`,typeof value==='number'?value.toFixed(2):'—');
  $(`${kind}-meter`).style.width=`${Math.min(100,Math.max(0,Number(value)||0))}%`;
 }
 $('overview-level').style.width=`${Math.min(100,Math.max(0,Number(lv.aec)||0))}%`;
 text('raw-age',health?.raw_age===null||health?.raw_age===undefined?'尚无数据':`${health.raw_age.toFixed(2)} 秒前更新`);
 text('aec-age',health?`${health.capture_age.toFixed(2)} 秒前更新`:'尚无数据');
 text('buffer-ms',health?`${health.queued_audio_ms} ms`:'—');
 text('dropped-frames',health?.dropped_frames??'—');
 const timing=health?.timing||{};
 text('turn-at',timing.wake_at?clock(timing.wake_at):'—');
 text('asr-delay',timing.asr_ready_ms===undefined?'—':`${timing.asr_ready_ms} ms`);
 text('text-delay',timing.first_text_ms===undefined?'—':`${timing.first_text_ms} ms`);
 const source={raw:'原始流唤醒',AEC:'消回声流唤醒',button:'手动唤醒',followup:'追问聆听'}[timing.source];
 pill('turn-source',source||'等待唤醒');
}
function renderServices(services){
 for(const [key,[title,description]] of Object.entries(serviceNames)){
  let card=$(`service-${key}`);
  if(!card){
   card=document.createElement('article');card.className='panel';card.id=`service-${key}`;
   card.innerHTML=`<div class="section-title"><h3>${title}</h3><span id="service-state-${key}" class="pill neutral">—</span></div><p>${description}</p><div class="service-metrics"><span>内存占用<strong id="service-memory-${key}">—</strong></span><span>自动重启<strong id="service-restarts-${key}">—</strong></span></div>`;
   if(key!=='gateway'){const button=document.createElement('button');button.textContent=`重启${title}`;button.addEventListener('click',()=>mutate('/api/restart',{target:key},button));card.append(button);}
   else {const note=document.createElement('small');note.className='muted';note.textContent='网页渲染在浏览器中完成';card.append(note);}
   $('service-cards').append(card);
  }
  const item=services[key]||{};const active=item.state==='active';
  pill(`service-state-${key}`,active?'运行中':({inactive:'未运行',failed:'启动失败',activating:'正在启动'}[item.state]||'未知'),active?'green':'amber');
  text(`service-memory-${key}`,mb(item.memory));text(`service-restarts-${key}`,item.restarts??'—');
 }
}
function render(data){
 latest=data;const h=data.health;const s=data.snapshot||{};const services=data.services||{};
 $('connection-error').hidden=true;
 pill('connection',h?.healthy?({idle:'待机 · 已连接',wake:'唤醒成功',listen:'正在聆听',think:'正在思考',answer:'正在回答',speak:'正在播报'}[h.phase]||'设备已连接'):h?.control_healthy?(h.audio_recovery?.status==='recovering'?'音频正在恢复':'等待音频就绪'):h?'语音响应异常':'语音服务离线',h?.healthy?'green':'amber');
 text('refreshed',`${clock(data.time)} 更新`);text('footer-time',clock(data.time));text('version',`网关 v${data.version}`);text('art-time',clock(data.time).slice(0,5));
 text('phase-title',h?(phaseNames[h.phase]||'设备正在工作'):'语音服务暂时离线');
 text('phase-detail',!h?'可在服务维护中尝试重新启动语音服务。':h.mic?'可以直接对设备说话，或在这里手动唤醒。':h.hardware_mic===false?'机身麦克风开关已关闭，请先拨开设备侧面的开关。':'软件麦克风已关闭，可通过下方按钮重新开启。');
 text('wake-label',`唤醒词「${data.wake_word}」`);text('hardware-label',`硬件麦克风 ${h?.hardware_mic===true?'开启':h?.hardware_mic===false?'关闭':'未知'}`);
 text('mic-button',data.preferences?.mic?'关闭麦克风':'开启麦克风');
 text('mic-title',h?(h.mic?'正在采集':h.hardware_mic===false?'机身开关关闭':'软件收音关闭'):'暂无数据');
 const wakeCount=h?.wake_detection?.count;
 const captureClock=h?.capture_clock;
 const inputBad=captureClock&&captureClock.ratio<.9;
 if(inputBad)pill('connection','收音数据不连续','amber');
 const recovery=h?.audio_recovery;
 const recoveryText=recovery?.status==='recovering'?' · 正在自动恢复':recovery?.status==='limited'?' · 自动恢复已达频率上限':recovery?.status==='failed'?' · 上次恢复未就绪':'';
 text('capture-continuity',(captureClock?`收音连续性：经过 ${captureClock.wall_seconds.toFixed(2)} 秒，实际收到 ${captureClock.audio_seconds.toFixed(2)} 秒音频${inputBad?' · 输入异常':''}`:'正在观察收音连续性')+recoveryText);
 text('capture-note',h?`采集更新 ${h.capture_age.toFixed(2)} 秒前 · 唤醒命中 ${wakeCount??'—'} 次`:'等待语音服务响应');
 text('wake-model',h?.models?.wake_model?`本地唤醒模型：${h.models.wake_model} · 本次运行命中 ${wakeCount??0} 次`:'等待语音服务返回唤醒模型');
 const turn=h?.turn_models||{};const actualModel=turn.chat_model||h?.models?.chat_model;
 text('model-name',modelName(turn.role==='expert'?'expert':'chat',actualModel));text('model-provider',h?`${turn.chat_model?'最近一轮':'日常对话'} · ${serviceName(turn.chat_provider||h.models?.chat_provider)}`:'语音服务离线');
 renderModelState();
 text('load',data.load?.[0]?.toFixed(2)||'—');
 const memory=Object.values(services).reduce((sum,item)=>sum+(item.memory||0),0);text('memory-note',`相关服务共使用 ${mb(memory)}`);
 const music=s.music||{};text('music-title',music.t||'当前没有播放音乐');text('music-artist',music.a||'通过语音点歌，或继续上次的播放。');pill('music-state',{playing:'播放中',paused:'已暂停',loading:'加载中',stopped:'已停止'}[music.s]||'未播放',music.s==='playing'?'green':'neutral');
 const sleepRemain=music.sleep&&music.sleep>data.time?Math.max(1,Math.ceil((music.sleep-data.time)/60)):0;
 text('sleep-button',sleepRemain?`伴睡剩余 ${sleepRemain} 分 · 取消`:'伴睡 30 分');
 if(document.activeElement!==$('volume')&&data.volume!==null){$('volume').value=data.volume;text('volume-output',`${data.volume}%`);}
 text('user-text',s.partial||s.user_text||'暂无对话内容');text('answer-text',s.answer||'唤醒设备后，对话会显示在这里。');
 $('alarm-box').hidden=!s.alarm_active;text('alarm-text',s.alarm_text||'');
 renderAlarms(s.alarm_items||[],data.time);
 text('restart-count',services.voice?.restarts??'—');levels(h);renderServices(services);
}
function renderAlarms(items,nowSec){
 const box=$('alarm-list');box.replaceChildren();
 if(!items.length){box.textContent='暂无待响闹钟或计时';return;}
 const names={alarm:'闹钟',timer:'计时器',reminder:'提醒'};
 for(const item of items){
  const row=document.createElement('div');row.className='alarm-item';
  const info=document.createElement('span');
  const remain=item.kind==='timer'&&item.due?Math.max(0,Math.round(item.due-nowSec)):null;
  const when=remain!==null?`${Math.floor(remain/60).toString().padStart(2,'0')}:${(remain%60).toString().padStart(2,'0')}`:(item.time||'');
  const parts=[names[item.kind]||'提醒',when,item.repeat,item.label].filter(Boolean);
  info.textContent=parts.join(' · ');
  const btn=document.createElement('button');btn.textContent='取消';
  btn.addEventListener('click',()=>mutate('/api/action',{action:'alarm_cancel',id:item.id},btn));
  row.append(info,btn);box.append(row);
 }
}
async function poll(){
 if(polling||document.hidden)return;polling=true;
 try{render(await api('/api/status'));}
 catch(error){pill('connection','连接已断开','red');$('connection-error').hidden=false;text('refreshed','请检查设备或重新打开管理入口');}
 finally{polling=false;}
}
const hhmm=minutes=>`${Math.floor(minutes/60).toString().padStart(2,'0')}:${(minutes%60).toString().padStart(2,'0')}`;
const minutes=value=>{const [h,m]=value.split(':').map(Number);return h*60+m;};
const serviceName=provider=>providerNames[provider]||provider||'未配置';
function modelName(slot,id){return (voiceBase?._catalog||latest?.catalog)?.[slot]?.find(m=>m.id===id)?.name||id||'未配置';}
function voiceName(model,id){return (voiceBase?._catalog||latest?.catalog)?.tts?.find(m=>m.id===model)?.voices.find(v=>v.id===id)?.name||id||'未配置';}
function selectOptions(select,choices,value){
 select.replaceChildren();const groups=new Map();
 for(const item of choices){
  const option=new Option(item.service?`${item.name} · ${item.service}`:item.name,item.id);
  if(item.group){const label=`${item.service} · ${item.group}`;if(!groups.has(label)){const group=document.createElement('optgroup');group.label=label;groups.set(label,group);select.add(group);}groups.get(label).append(option);}
  else select.add(option);
 }
 if(value&&!choices.some(item=>item.id===value)){const option=new Option(`${value} · 当前配置（不在支持列表）`,value);option.disabled=true;select.add(option);}
 select.value=value||choices[0]?.id||'';
}
function syncVoices(wanted){
 const form=$('model-form').elements;const model=voiceBase?._catalog?.tts?.find(m=>m.id===form.tts_model.value);
 const choices=model?.voices||[];const previous=wanted??form.tts_voice.value;
 selectOptions(form.tts_voice,choices,choices.some(v=>v.id===previous)?previous:choices[0]?.id);
 form.tts_voice.disabled=!choices.length;$('preview-voice').disabled=!choices.length;
 text('voice-compatibility',choices.length?`${choices.length} 种兼容中文音色 · ${form.tts_voice.value}`:'当前模型暂无已核实的兼容音色，请选择合成模型');
}
function modelNotes(){
 const form=$('model-form').elements;
 const selected=voiceBase._catalog?.chat?.find(m=>m.id===form.chat_model.value);
 form.chat_provider.value=selected?.provider||voiceBase.chat_provider;
 text('configured-provider',`${serviceName(form.chat_provider.value)} · ${form.chat_model.value}`);
 text('expert-note',voiceBase.expert_configured?`${serviceName(voiceBase.expert_provider)} · ${form.expert_model.value}`:'尚未配置复杂任务模型凭据');
}
function fillModels(){
 const form=$('model-form').elements;const catalog=voiceBase._catalog||{};
 selectOptions(form.chat_model,catalog.chat||[],voiceBase.chat_model);
 selectOptions(form.expert_model,catalog.expert||[],voiceBase.expert_model);form.expert_model.disabled=!voiceBase.expert_configured;
 selectOptions(form.tts_model,catalog.tts||[],voiceBase.tts_model);syncVoices(voiceBase.tts_voice);modelNotes();renderModelState();
}
function renderModelState(){
 const health=latest?.health, models=health?.models, turn=health?.turn_models||{};
 text('effective-chat',models?`${modelName('chat',models.chat_model)} · ${serviceName(models.chat_provider)}`:'语音服务离线，无法确认');
 text('effective-expert',models?.expert_model?`${modelName('expert',models.expert_model)} · ${serviceName(models.expert_provider)}`:health?'未配置':'语音服务离线，无法确认');
 text('effective-tts',models?`${modelName('tts',models.tts_model)} · 阿里云百炼`:'语音服务离线，无法确认');
 text('effective-voice',models?`${voiceName(models.tts_model,models.tts_voice)} · ${models.tts_voice}`:'语音服务离线，无法确认');
 const used=[];
 if(turn.chat_model)used.push(`${turn.role==='expert'?'复杂任务':'日常对话'}：${modelName(turn.role||'chat',turn.chat_model)} · ${serviceName(turn.chat_provider)}`);
 else if(turn.started_at)used.push('本轮尚未调用对话模型');
 if(turn.speech==='local')used.push('实际播报：本地语音（espeak-ng）');
 else if(turn.speech==='cloud')used.push(`实际播报：${voiceName(turn.tts_model,turn.tts_voice)} · ${modelName('tts',turn.tts_model)}`);
 if(turn.error)used.push('本轮未完成，请查看语音诊断');
 text('turn-models',used.length?`最近一轮 · ${used.join('；')}`:'暂无本轮使用记录');
 if(!voiceBase)return;
 const form=$('model-form').elements;
 const keys=modelFields.filter(key=>key!=='expert_model'||voiceBase.expert_configured);
 const dirty=keys.some(key=>form[key].value!==voiceBase[key]);
 const pending=models&&keys.some(key=>models[key]!==voiceBase[key]);
 if(modelSaving){pill('model-update-state','正在保存','amber');text('model-config-state','正在保存模型与音色，当前播报继续');return;}
 pill('model-update-state',dirty?'有未保存修改':!health?'等待语音服务':pending?'下一轮生效':'已生效',dirty||!health||pending?'amber':'green');
 text('model-config-state',dirty?'所选内容尚未保存；试听不会保存设置':!health?'已保存的配置将在语音服务恢复后使用，尚未确认生效':pending?'已保存，等待下一轮对话；当前播报继续':'已保存配置与设备运行值一致，后续对话将使用以上配置');
}
function stopPreview(){
 previewSequence++;previewController?.abort();previewController=null;
 const audio=$('voice-audio');audio.pause();audio.removeAttribute('src');audio.load();audio.hidden=true;
 if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=null;
 $('preview-voice').disabled=!$('model-form').elements.tts_voice.value;
 text('preview-voice','试听所选音色');text('preview-state','点击后在这台电脑播放，试听不会保存设置');
}
async function previewVoice(){
 stopPreview();const sequence=previewSequence;const controller=new AbortController();previewController=controller;
 const form=$('model-form').elements;const chosen={model:form.tts_model.value,voice:form.tts_voice.value};
 $('preview-voice').disabled=true;text('preview-voice','正在生成试听…');text('preview-state','正在使用所选模型生成试听，不影响设备当前播报');
 const timer=setTimeout(()=>controller.abort(),45000);
 try{
  if(!csrf)csrf=(await api('/api/session')).csrf;
  let response;
  for(let attempt=0;attempt<2;attempt++){
   response=await fetch('/api/preview',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(chosen),signal:controller.signal,credentials:'same-origin'});
   if(response.status!==403||attempt)break;csrf=(await api('/api/session')).csrf;
  }
  if(!response.ok){const error=await response.json();throw new Error(error.error||'试听生成失败');}
  const blob=await response.blob();if(sequence!==previewSequence)return;
  previewUrl=URL.createObjectURL(blob);const audio=$('voice-audio');audio.src=previewUrl;audio.hidden=false;
  text('preview-state',`试听：${voiceName(chosen.model,chosen.voice)} · 未保存`);
  try{await audio.play();}catch{text('preview-state','试听已准备好，请点击播放器播放');}
 }catch(error){if(sequence===previewSequence){const message=error.name==='AbortError'?'试听生成超时，请重试':error.message;text('preview-state',message);toast(message,true);}}
 finally{clearTimeout(timer);if(sequence===previewSequence){previewController=null;$('preview-voice').disabled=false;text('preview-voice','重新试听');}}
}
function fillSettings(data){
 prefsTouched.clear();
 prefsBase=data.preferences;voiceBase=data.voice;
 const form=$('preferences-form');
 for(const name of ['speech_rate','brightness','dim_after']){
  const value=prefsBase[name]??80;
  if(name==='speech_rate'&&!Array.from(form.elements[name].options).some(o=>Number(o.value)===Number(value)))form.elements[name].add(new Option(`${value} 倍`,value));
  form.elements[name].value=value;
 }
 for(const name of ['mic','thinking'])form.elements[name].checked=!!prefsBase[name];
 form.elements.night_enabled.checked=!!prefsBase.night.enabled;form.elements.night_start.value=hhmm(prefsBase.night.start);form.elements.night_end.value=hhmm(prefsBase.night.end);
 const voiceForm=$('voice-form');for(const key of [...numericVoice,'asr_model'])voiceForm.elements[key].value=voiceBase[key]??'';
 fillModels();
 $('wake-word').value=latest?.wake_word||'';settingsLoaded=true;
}
async function loadSettings(){try{fillSettings(await api('/api/settings'));}catch(error){toast(error.message,true);}}
async function loadLogs(){
 if(loadingLogs)return;loadingLogs=true;logsAt=Date.now();
 try{
  const data=await api(`/api/logs?target=${encodeURIComponent($('log-source').value)}`);$('logs').replaceChildren();
  if(!data.rows.length){$('logs').textContent='最近 15 分钟暂无日志。';return;}
  for(const item of data.rows){const row=document.createElement('div');row.className=`log-row ${item.level}`;const at=document.createElement('time');at.textContent=clock(item.time);const content=document.createElement('span');content.textContent=item.message;row.append(at,content);$('logs').append(row);}
 }catch(error){$('logs').textContent=error.message;}finally{loadingLogs=false;}
}
document.querySelectorAll('[data-page]').forEach(button=>button.addEventListener('click',()=>setPage(button.dataset.page)));
document.querySelectorAll('[data-action]').forEach(button=>button.addEventListener('click',()=>mutate('/api/action',{action:button.dataset.action},button)));
document.querySelectorAll('[data-timer]').forEach(button=>button.addEventListener('click',()=>mutate('/api/action',{action:'timer',minutes:Number(button.dataset.timer)},button)));
$('sleep-button').addEventListener('click',()=>{const sleep=latest?.snapshot?.music?.sleep;const active=sleep&&sleep>(latest?.time||0);mutate('/api/action',{action:'sleep_timer',minutes:active?0:30},$('sleep-button'));});
$('mic-button').addEventListener('click',async()=>{if(!latest)return;const result=await mutate('/api/preferences',{mic:!latest.preferences.mic},$('mic-button'));if(result)settingsLoaded=false;});
$('volume').addEventListener('input',event=>text('volume-output',`${event.target.value}%`));
$('volume').addEventListener('change',()=>mutate('/api/action',{action:'volume',value:Number($('volume').value)}));
$('preferences-form').addEventListener('submit',async event=>{
 event.preventDefault();if(!prefsBase)return;const f=event.currentTarget.elements;
 const values={speech_rate:Number(f.speech_rate.value),brightness:Number(f.brightness.value),dim_after:Number(f.dim_after.value),mic:f.mic.checked,thinking:f.thinking.checked,night:{enabled:f.night_enabled.checked,start:minutes(f.night_start.value),end:minutes(f.night_end.value)}};
 const changed=Object.fromEntries(Object.entries(values).filter(([k,v])=>prefsTouched.has(k)&&JSON.stringify(v)!==JSON.stringify(prefsBase[k])));
 if(!Object.keys(changed).length)return toast('设置没有变化');
 const result=await mutate('/api/preferences',changed,event.submitter);if(result){prefsBase=result.preferences;prefsTouched.clear();text('preferences-state',result.status==='applied'?'已保存并应用':'已保存，等待语音服务恢复');}
});
for(const eventName of ['input','change'])$('preferences-form').addEventListener(eventName,event=>{const key=event.target.name;if(key)prefsTouched.add(key.startsWith('night_')?'night':key);});
$('voice-form').addEventListener('submit',async event=>{
 event.preventDefault();if(!voiceBase)return;const form=event.currentTarget;const values={};
 for(const key of [...numericVoice,'asr_model']){const value=numericVoice.includes(key)?Number(form.elements[key].value):form.elements[key].value.trim();if(value!==voiceBase[key])values[key]=value;}
 if(!Object.keys(values).length)return toast('语音配置没有变化');
 text('voice-config-state','正在应用配置并等待语音服务就绪…');
 const result=await mutate('/api/configure',values,event.submitter);text('voice-config-state',result?'识别与唤醒参数已生效，语音服务已就绪':'未确认配置生效，请查看提示');if(result){for(const key of Object.keys(values))voiceBase[key]=result.voice[key];renderModelState();}
});
$('model-form').addEventListener('submit',async event=>{
 event.preventDefault();if(!voiceBase||busy)return;const form=event.currentTarget.elements;const values={};
 for(const key of modelFields)if(!form[key].disabled&&form[key].value!==voiceBase[key])values[key]=form[key].value;
 if(!Object.keys(values).length)return toast('模型与音色没有变化');
 modelSaving=true;renderModelState();
 const result=await mutate('/api/configure',values,event.submitter);modelSaving=false;
 if(result){voiceBase=result.voice;modelNotes();renderModelState();}else{pill('model-update-state','保存未确认','red');text('model-config-state','尚未确认保存结果，请重新读取设置后核对');}
});
$('model-form').addEventListener('change',event=>{
 if(!voiceBase||!modelFields.includes(event.target.name))return;
 if(['tts_model','tts_voice'].includes(event.target.name))stopPreview();
 if(event.target.name==='tts_model')syncVoices();
 if(event.target.name==='tts_voice')text('voice-compatibility',`兼容音色 · ${event.target.value}`);
 modelNotes();renderModelState();
});
$('preview-voice').addEventListener('click',previewVoice);
$('wake-form').addEventListener('submit',async event=>{event.preventDefault();await mutate('/api/wake-word',{phrase:$('wake-word').value.trim()},event.submitter);});
$('refresh-logs').addEventListener('click',loadLogs);$('log-source').addEventListener('change',loadLogs);
window.addEventListener('hashchange',()=>{const next=location.hash.slice(1);if(next!==page)setPage(next);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)poll();});
(async()=>{try{csrf=(await api('/api/session')).csrf;}catch{}await poll();setPage(location.hash.slice(1)||'overview');})();
setInterval(()=>{poll();if(page==='diagnostics'&&!document.hidden&&Date.now()-logsAt>10000)loadLogs();},2000);
