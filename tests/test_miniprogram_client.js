// Exercise native-page logic without a WeChat account or notification sends.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const { decorate } = require('../miniprogram/utils/results');
const day='2026-10-01';
const result=decorate({quotes:[
  {departure_date:day,price:500,currency:'CNY',comparable:true,source:'携程'},
  {departure_date:day,price:400,currency:'CNY',comparable:true,source:'去哪儿'},
  {departure_date:day,price:1,currency:'CNY',comparable:false,source:'城市参考'},
  {departure_date:'2026-10-02',price:700,currency:'CNY',comparable:true,source:'携程'}],
  sources:[{id:'qunar',status:'reference',city_reference_quotes:[{price:1}]}]});
assert.equal(result[0].daily.length,2);
assert.equal(result[0].best.price,400);
assert.equal(result[0].daily[0].source,'去哪儿');
assert.equal(result[0].sources[0].label,'仅城市参考');
let definition, subscriptionCalls=0;
const requests=[];
const app={token:'test-session',request:async(...args)=>{requests.push(args);return {ok:true};}};
const wx={setStorageSync(){},showToast(){},requestSubscribeMessage(options){
  subscriptionCalls++;options.success({'real-template':'accept'});
}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../miniprogram/pages/index/index.js'),'utf8'),{
  require:()=>({decorate}),getApp:()=>app,Page:value=>{definition=value;},wx,
  setTimeout,clearTimeout,console
});
const page={...definition,data:JSON.parse(JSON.stringify(definition.data)),setData(update){
  for(const [key,value] of Object.entries(update)){
    const parts=key.split('.');let target=this.data;
    for(const name of parts.slice(0,-1))target=target[name];
    target[parts.at(-1)]=value;
  }
}};
page.data.providers=[{id:'ctrip',name:'携程'},{id:'qunar',name:'去哪儿'}];
page.data.today=day;page.resetForm();
assert.equal(subscriptionCalls,0,'constructing a page must never open subscription dialog');
page.pick({currentTarget:{dataset:{side:'origin'}}});
page.data.places=[{code:'SHA',scope:'city',city_code:'SHA',label:'上海（全部机场）',market:'domestic'},
 {code:'PVG',scope:'airport',city_code:'SHA',label:'浦东机场',market:'domestic'}];
page.choosePlace({currentTarget:{dataset:{index:1}}});
assert.equal(page.data.form.origin,'PVG');
assert.equal(page.data.form.origin_scope,'airport');
assert.equal(page.data.form.origin_city_code,'SHA');
Object.assign(page.data.form,{destination:'CJU',destination_city_code:'CJU',destination_scope:'airport',destination_label:'济州机场'});
page.addTrip();
assert.equal(page.data.trips.length,1);
assert.equal(page.data.trips[0].start_date,page.data.trips[0].end_date);
assert.equal('origin_market' in page.data.trips[0],false,'UI-only fields must not break strict backend validation');
assert.equal(page.cleanTrip({...page.data.trips[0],mode:'lowest',threshold:null}).threshold,null);
assert.equal(subscriptionCalls,0,'adding a trip does not prompt or grant subscription');
page.templateId='real-template';page.subscribe();
assert.equal(subscriptionCalls,1);
setImmediate(()=>{
 assert.equal(requests[0][0],'/api/subscribe');
 assert.equal(requests[0][1].template_id,'real-template');
 assert.equal(page.data.subscription,'accepted');
 console.log('PASS: day minima, city-reference exclusion, airport identity, clean multitrip payload, user-tap subscription');
});
