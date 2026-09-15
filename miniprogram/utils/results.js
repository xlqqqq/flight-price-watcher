function decorate(latest) {
  if (!latest) return [];
  return (latest.trips || [latest]).map(trip => {
    const days = {};
    for (const q of trip.quotes || []) {
      if (!q.comparable || q.currency !== 'CNY' || !Number.isFinite(q.price) || q.price <= 0) continue;
      if (!days[q.departure_date] || q.price < days[q.departure_date].price) days[q.departure_date] = q;
    }
    const quotes = Object.keys(days).sort().map(day => days[day]);
    const lowest = quotes.reduce((best, q) => !best || q.price < best.price ? q : best, null);
    const labels = {ok:'已返回报价', reference:'仅城市参考', error:'查询失败', unsupported:'暂不支持', empty:'暂无报价', pending:'查询中'};
    return { ...trip, daily: quotes, best: lowest, sources: (trip.sources || []).map(s => ({...s, label:labels[s.status] || '待确认'})) };
  });
}
module.exports = { decorate };
