const { decorate } = require('../../utils/results');
const app = getApp();
Page({
  data: { logged: false, busy: false, pairing: '', error: '', trips: [], providers: [], results: [],
    form: {}, modes: ['最低价汇总', '低于目标价', '两者都提醒'], modeIndex: 2,
    interval: 360, monitor: {}, pickerSide: '', places: [], placeQuery: '',
    subscription: 'needed', sourceDetails: false, editing: -1 },
  onShow() { if (app.token) { if(this.initialized){this.hidden=false;this.refresh();}else this.load(); } },
  onHide() { this.cancelTimers(); },
  onUnload() { this.cancelTimers(); },
  cancelTimers() { clearTimeout(this.poll); clearTimeout(this.placeTimer); this.hidden = true; },
  async load() {
    this.hidden = false;
    try {
      const boot = await app.request('/api/bootstrap');
      this.templateId = boot.template_id;
      this.initialized = true;
      const saved = boot.defaults;
      const trips = saved.trips || (saved.origin ? [saved] : wx.getStorageSync('miniDraftTrips') || []);
      this.setData({logged:true, providers:boot.providers, today:boot.today, maxDate:boot.max_date,
        trips, interval:saved.interval_minutes || 360, subscriptionNote:boot.subscription_note});
      this.resetForm(); this.refresh();
    } catch (e) { this.fail(e); }
  },
  fail(e) { this.setData({error:e.message || '操作失败', logged:!!app.token}); },
  pairingInput(e) { this.setData({pairing:e.detail.value}); },
  async login() {
    if (this.data.busy) return;
    this.setData({busy:true,error:''});
    try { await app.login(this.data.pairing); this.setData({pairing:''}); await this.load(); }
    catch(e) {this.fail(e);} finally {this.setData({busy:false});}
  },
  resetForm() {
    const day = this.data.today;
    this.setData({ editing:-1, modeIndex:2, form:{ origin:'',destination:'',origin_label:'',destination_label:'',
      origin_scope:'city',destination_scope:'city',start_date:day,end_date:day,market:'international',
      mode:'both',threshold:600,providers:this.data.providers.map(p=>p.id) } });
    this.syncProviders();
  },
  syncProviders() { this.setData({providerChoices:this.data.providers.map(p=>({...p,checked:this.data.form.providers.includes(p.id)}))}); },
  field(e) { this.setData({['form.'+e.currentTarget.dataset.field]:e.detail.value}); },
  intervalInput(e) { this.setData({interval:e.detail.value}); },
  mode(e) { const i=Number(e.detail.value);this.setData({modeIndex:i,'form.mode':['lowest','threshold','both'][i]}); },
  market(e) {this.setData({'form.market':Number(e.detail.value)===0?'domestic':'international'});},
  providers(e) {this.setData({'form.providers':e.detail.value});},
  pick(e) {this.placeGeneration=(this.placeGeneration||0)+1;this.setData({pickerSide:e.currentTarget.dataset.side,places:[],placeQuery:''});},
  closePicker() {clearTimeout(this.placeTimer);this.placeGeneration=(this.placeGeneration||0)+1;this.setData({pickerSide:''});},
  searchPlace(e) {
    const query=e.detail.value.trim();this.setData({placeQuery:query});clearTimeout(this.placeTimer);
    const generation=++this.placeGeneration;
    if(!query){this.setData({places:[]});return;}
    this.placeTimer=setTimeout(async()=>{
      try {const res=await app.request('/api/cities?q='+encodeURIComponent(query));
        if(generation===this.placeGeneration && this.data.pickerSide)this.setData({places:res.cities.map(p=>({...p,label:p.label || `${p.name}（${p.code} · 全部机场）`}))});
      }catch(e){this.fail(e);}
    },350);
  },
  choosePlace(e) {
    const place=this.data.places[Number(e.currentTarget.dataset.index)],side=this.data.pickerSide;
    if(!place || !side)return;
    this.setData({['form.'+side]:place.code,['form.'+side+'_scope']:place.scope||'city',
      ['form.'+side+'_city_code']:place.city_code||place.code,['form.'+side+'_label']:place.label,
      ['form.'+side+'_market']:place.market});
    const f=this.data.form;
    if(f.origin_market && f.destination_market)this.setData({'form.market':f.origin_market==='domestic' && f.destination_market==='domestic'?'domestic':'international'});
    this.closePicker();
  },
  cleanTrip(f) {
    const keys=['origin','destination','origin_scope','destination_scope','origin_city_code','destination_city_code',
      'origin_label','destination_label','market','start_date','end_date','mode','threshold','providers'];
    const out={};keys.forEach(k=>{if(f[k]!==undefined)out[k]=f[k];});
    out.threshold=out.threshold==='' || out.threshold==null?null:Number(out.threshold);return out;
  },
  addTrip() {
    if(this.data.monitor.running)return this.fail(new Error('请先停止监控再修改行程'));
    const f=this.cleanTrip(this.data.form);
    if(!f.origin || !f.destination || !f.providers.length)return this.fail(new Error('请选择出发地、目的地和查询平台'));
    if(f.end_date<f.start_date)return this.fail(new Error('结束日期不能早于开始日期'));
    const trips=this.data.trips.slice();
    if(this.data.editing>=0)trips[this.data.editing]=f;
    else {if(trips.length>=10)return this.fail(new Error('最多同时监控 10 个行程'));trips.push(f);}
    this.setData({trips,error:''});wx.setStorageSync('miniDraftTrips',trips);this.resetForm();
  },
  editTrip(e) {
    if(this.data.monitor.running)return;
    const index=Number(e.currentTarget.dataset.index),f=this.data.trips[index];
    this.setData({editing:index,form:{...f},modeIndex:['lowest','threshold','both'].indexOf(f.mode)});this.syncProviders();
  },
  removeTrip(e) {
    if(this.data.monitor.running)return;
    const trips=this.data.trips.filter((_,i)=>i!==Number(e.currentTarget.dataset.index));
    this.setData({trips});wx.setStorageSync('miniDraftTrips',trips);this.resetForm();
  },
  async action(e) {
    const action=e.currentTarget.dataset.action;
    if(this.data.busy)return;
    this.setData({busy:true,error:''});
    try {
      if(action!=='stop' && !this.data.trips.length)throw new Error('请先把行程加入清单');
      await app.request('/api/'+action,action==='stop'?{}:{trips:this.data.trips.map(t=>this.cleanTrip(t)),interval_minutes:Number(this.data.interval)},'POST');
      wx.showToast({title:action==='save'?'已保存':'已提交',icon:'success'});await this.refresh();
    } catch(e){this.fail(e);} finally{this.setData({busy:false});}
  },
  subscribe() {
    // Must run directly from the user's tap; never request on load or a timer.
    if(!this.templateId)return;
    wx.requestSubscribeMessage({tmplIds:[this.templateId],success:async res=>{
      const result=res[this.templateId];
      if(!['accept','reject','ban'].includes(result))return;
      try{await app.request('/api/subscribe',{template_id:this.templateId,result},'POST');
        this.setData({subscription:result==='accept'?'accepted':'needed'});
        wx.showToast({title:result==='accept'?'已提交订阅授权':'未开启提醒',icon:'none'});
      }catch(e){this.fail(e);}
    },fail:()=>this.fail(new Error('订阅未完成，请在手机微信中操作并检查模板配置'))});
  },
  async refresh() {
    clearTimeout(this.poll);
    try {const s=await app.request('/api/status');this.setData({monitor:s.monitor,searchBusy:s.search_busy,
      results:decorate(s.latest),subscription:s.subscription,notifications:s.notifications||[]});}
    catch(e){this.fail(e);}
    if(!this.hidden && app.token)this.poll=setTimeout(()=>this.refresh(),5000);
  },
  toggleSources(){this.setData({sourceDetails:!this.data.sourceDetails});},
  copy(e){const url=e.currentTarget.dataset.url;if(/^https:\/\//.test(url||''))wx.setClipboardData({data:url});}
});
