const app=getApp();
Page({
  data:{message:null,error:''},
  onLoad(options){this.id=options.id;this.load();},
  async load(){
    try{if(!/^[a-f0-9]{32}$/.test(this.id||''))throw new Error('提醒链接无效');
      if(!app.token)await app.login('');
      this.setData({message:await app.request('/api/messages/'+this.id),error:''});
    }catch(e){this.setData({error:e.message});}
  },
  async retry(){try{await app.login('');await this.load();}catch(e){this.setData({error:e.message});}},
  copy(){const url=this.data.message.url;if(/^https:\/\//.test(url||''))wx.setClipboardData({data:url});},
  home(){wx.reLaunch({url:'/pages/index/index'});}
});
