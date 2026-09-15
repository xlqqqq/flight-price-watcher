const { apiBase } = require('./config');
App({
  token: '',
  onLaunch() { this.token = wx.getStorageSync('miniSession') || ''; },
  request(path, data, method = 'GET') {
    return new Promise((resolve, reject) => {
      if (!/^https:\/\//.test(apiBase) || apiBase.includes('YOUR-')) return reject(new Error('请先配置小程序后端 HTTPS 域名'));
      wx.request({ url: apiBase.replace(/\/$/, '') + path, method, data, timeout: 25000,
        header: { 'content-type': 'application/json', Authorization: 'Bearer ' + this.token },
        success: res => {
          if (res.statusCode === 401) { this.token = ''; wx.removeStorageSync('miniSession'); }
          if (res.statusCode >= 200 && res.statusCode < 300) resolve(res.data);
          else reject(new Error((res.data || {}).error || '请求失败，请稍后重试'));
        }, fail: () => reject(new Error('连接失败，请检查后端域名及网络')) });
    });
  },
  async login(pairing) {
    const code = await new Promise((resolve, reject) => wx.login({
      success: res => res.code ? resolve(res.code) : reject(new Error('微信登录失败')),
      fail: () => reject(new Error('微信登录失败')) }));
    const result = await this.request('/api/login', { code, pairing: pairing || '' }, 'POST');
    this.token = result.token; wx.setStorageSync('miniSession', result.token);
  }
});
