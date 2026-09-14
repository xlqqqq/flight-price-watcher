"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const DAY = 86400000;
  const state = {
    bootstrap: null,
    status: null,
    pending: false,
    polling: false,
    initializedStatus: false,
    seenNotifications: new Set(),
    resultSignature: "",
    notificationSignature: "",
    cityCatalog: new Map(),
    cityPickers: {},
    airportLoads: new Map(),
    trips: [],
  };
  const MAX_TRIPS = 10;
  const priceFormat = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });

  function notice(message, error = false) {
    const element = $("action-message");
    element.textContent = message;
    element.className = error ? "notice notice-error" : "notice notice-success";
    element.hidden = !message;
  }

  function formError(message) {
    $("form-error").textContent = message;
    $("form-error").hidden = !message;
  }

  async function request(path, body) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const options = { signal: controller.signal, cache: "no-store", credentials: "same-origin" };
      if (body !== undefined) {
        options.method = "POST";
        options.headers = { "Content-Type": "application/json", "X-Flightwatch": "1" };
        options.body = JSON.stringify(body);
      }
      const response = await fetch(path, options);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `操作失败（HTTP ${response.status}）`);
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("本地服务响应超时，请稍后重试。");
      if (error instanceof TypeError) throw new Error("无法连接本地服务，请确认程序仍在运行。");
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  function placeIdentity(place) {
    if (!place || typeof place.code !== "string") return "";
    const scope = place.scope === "airport" ? "airport" : "city";
    const code = place.code.toUpperCase();
    const cityCode = String(place.city_code || (scope === "city" ? code : "")).toUpperCase();
    return /^[A-Z]{3}$/.test(code) && /^[A-Z]{3}$/.test(cityCode) ? `${scope}:${code}:${cityCode}` : "";
  }

  function cityLabel(place) {
    if (typeof place?.label === "string" && place.label.trim()) return place.label.trim();
    const code = String(place?.code || "").toUpperCase();
    const name = String(place?.name || place?.city_name || code);
    return place?.scope === "airport" ? `${name}（${code}）` : `${name}（${code} · 全部机场）`;
  }

  function selectedPlace(input) {
    const identity = input?.dataset?.placeIdentity || "";
    const place = state.cityCatalog.get(identity);
    if (place && input.value === cityLabel(place)) return place;
    if (input?.dataset) delete input.dataset.placeIdentity;
    return null;
  }

  function cityFor(value) {
    const normalized = String(value || "").trim().toUpperCase();
    const places = Array.from(state.cityCatalog.values());
    return places.find((place) => cityLabel(place).toUpperCase() === normalized)
      || places.find((place) => place.scope === "city" && place.code === normalized)
      || places.find((place) => String(place.name || "").toUpperCase() === normalized);
  }

  function cityName(code) { return cityFor(code)?.city_name || cityFor(code)?.name || code || "—"; }

  function normalizeCity(input) {
    const city = selectedPlace(input) || cityFor(input.value);
    if (city) {
      input.value = cityLabel(city);
      input.dataset.placeIdentity = placeIdentity(city);
    }
    state.cityPickers[input.id]?.syncClear();
    return city;
  }

  function mergeCities(cities) {
    if (!Array.isArray(cities)) return [];
    const accepted = [];
    const identities = new Set();
    for (const city of cities) {
      if (!city || typeof city.code !== "string" || !/^[A-Za-z]{3}$/.test(city.code) || typeof city.name !== "string" || !city.name.trim()) continue;
      const scope = city.scope === "airport" ? "airport" : "city";
      const code = city.code.toUpperCase();
      const cityCode = String(city.city_code || (scope === "city" ? code : "")).toUpperCase();
      const normalized = { ...city, scope, code, city_code: cityCode, name: city.name.trim() };
      const identity = placeIdentity(normalized);
      if (!identity) continue;
      normalized.label = cityLabel(normalized);
      state.cityCatalog.set(identity, normalized);
      if (!identities.has(identity)) accepted.push(normalized);
      identities.add(identity);
    }
    return accepted;
  }

  function matchingLocalCities(query) {
    const normalized = String(query || "").trim().toLowerCase();
    const popular = state.bootstrap?.cities || [];
    if (!normalized) return popular.slice(0, 12);
    return Array.from(state.cityCatalog.values()).filter((city) => {
      const searchable = [city.name, city.code, city.label, city.city_name, city.city_code,
        city.airport_name, city.pinyin, city.en_name, ...(Array.isArray(city.aliases) ? city.aliases : [])];
      return searchable.some((value) => String(value || "").toLowerCase().includes(normalized));
    }).slice(0, 20);
  }

  function airportChoices(place) {
    const owner = place.city_code;
    let city = state.cityCatalog.get(`city:${owner}:${owner}`);
    if (!city) {
      city = mergeCities([{ scope: "city", code: owner, city_code: owner,
        name: place.city_name || owner, city_name: place.city_name || owner,
        market: place.market, country: place.country }])[0];
    }
    const airports = Array.from(state.cityCatalog.values())
      .filter((item) => item.scope === "airport" && item.city_code === owner)
      .sort((a, b) => a.name.localeCompare(b.name, "zh-CN"));
    return [city, ...airports];
  }

  async function loadCityAirports(place) {
    const owner = place.city_code;
    const load = { status: "loading" };
    state.airportLoads.set(owner, load);
    try {
      // City names avoid city/airport code collisions such as SHA/Hongqiao.
      const city = state.cityCatalog.get(`city:${owner}:${owner}`);
      const query = city?.city_name || city?.name || owner;
      const data = await request(`/api/cities?q=${encodeURIComponent(query)}`);
      const selected = ["origin", "destination"].map((id) => [id, selectedPlace($(id))]);
      const rows = mergeCities((data.cities || []).filter((item) =>
        (item.city_code || item.code) === owner));
      // Refresh labels without changing the selected place or airport scope.
      for (const [id, prior] of selected) {
        if (prior) $(id).value = cityLabel(state.cityCatalog.get(placeIdentity(prior)));
      }
      load.status = "loaded";
      load.warning = data.warning || (rows.some((item) => item.scope === "airport")
        ? "" : "暂未取得该城市的机场列表，可在上方直接搜索机场名称或代码。");
    } catch (error) {
      load.status = "error";
      load.warning = `${error.message} 已保留当前机场范围，可重试或直接搜索机场。`;
    }
    syncAirportSelectors();
  }

  function syncAirportSelectors() {
    for (const id of ["origin", "destination"]) {
      const place = selectedPlace($(id)) || cityFor($(id).value);
      $(`${id}-airport-field`).hidden = !place;
      if (!place) continue;
      const choices = airportChoices(place);
      const select = $(`${id}-airport`);
      select.replaceChildren(...choices.map((item) => {
        const option = element("option", "", item.scope === "city"
          ? `${item.city_name || item.name} · 全部机场`
          : `${item.name}（${item.code}）`);
        option.value = placeIdentity(item);
        return option;
      }));
      select.value = placeIdentity(place);
      let load = state.airportLoads.get(place.city_code);
      if (!load) {
        loadCityAirports(place);
        load = state.airportLoads.get(place.city_code);
      }
      $(`${id}-airport-hint`).textContent = load.status === "loading"
        ? "正在加载该城市的机场，当前选择保持不变…"
        : load.warning || (place.scope === "airport"
          ? `仅查询 ${place.name}（${place.code}）` : "比较该城市全部机场的航班");
      $(`${id}-airport-retry`).hidden = !load.warning;
    }
  }

  function createCityPicker(id) {
    const input = $(id);
    const wrapper = $(`${id}-field`);
    const popup = $(`${id}-popup`);
    const list = $(`${id}-options`);
    const status = $(`${id}-search-status`);
    const clear = $(`${id}-clear`);
    let options = [];
    let active = -1;
    let generation = 0;
    let debounce = null;
    let controller = null;
    let composing = false;
    let selectOnClick = false;
    let suppressFocusOpen = false;
    let optionNodes = [];

    function syncClear() { clear.hidden = !input.value; }

    function cancelSearch() {
      generation += 1;
      clearTimeout(debounce);
      debounce = null;
      controller?.abort();
      controller = null;
    }

    function close() {
      cancelSearch();
      popup.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }

    function markActive(index) {
      active = index;
      optionNodes.forEach((option, position) => {
        option.setAttribute("aria-selected", String(position === active));
        option.classList.toggle("active", position === active);
      });
      if (active < 0 || !optionNodes[active]) input.removeAttribute("aria-activedescendant");
      else {
        input.setAttribute("aria-activedescendant", optionNodes[active].id);
        optionNodes[active].scrollIntoView({ block: "nearest" });
      }
    }

    function choose(index) {
      const city = options[index];
      if (!city) return;
      input.value = cityLabel(city);
      input.dataset.placeIdentity = placeIdentity(city);
      syncClear();
      close();
      formError("");
      detectMarket();
      suppressFocusOpen = true;
      input.focus({ preventScroll: true });
      suppressFocusOpen = false;
    }

    function render(cities, message, warning = false) {
      options = [...cities].sort((a, b) => String(a.country || "").localeCompare(String(b.country || ""), "zh-CN")
        || String(a.city_name || a.name).localeCompare(String(b.city_name || b.name), "zh-CN")
        || Number(a.scope === "airport") - Number(b.scope === "airport")
        || String(a.name).localeCompare(String(b.name), "zh-CN"));
      active = -1;
      optionNodes = [];
      input.removeAttribute("aria-activedescendant");
      const fragment = document.createDocumentFragment();
      let previousGroup = "";
      options.forEach((city, index) => {
        const country = city.country || (city.market === "domestic" ? "中国" : "国际 / 港澳台");
        const group = `${country}\u0000${city.city_code}`;
        if (group !== previousGroup) {
          const groupLabel = element("li", "city-option-group", `${country} › ${city.city_name || city.name}`);
          groupLabel.setAttribute("role", "presentation");
          fragment.append(groupLabel);
          previousGroup = group;
        }
        const option = element("li", "city-option");
        option.id = `${id}-option-${index}`;
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        const name = element("span", "city-option-name", city.scope === "airport" ? city.name : `${city.city_name || city.name} · 全部机场`);
        const meta = element("span", "city-option-meta", city.scope === "airport"
          ? `具体机场 · ${city.code} · 所属城市 ${city.city_code}`
          : `城市范围 · ${city.code}`);
        option.append(name, meta);
        option.addEventListener("pointerdown", (event) => {
          cancelSearch();
          // Preserve combobox focus so pointer selection wins over the blur handler.
          if (event.pointerType !== "touch") event.preventDefault();
        });
        option.addEventListener("click", () => choose(index));
        optionNodes.push(option);
        fragment.append(option);
      });
      list.replaceChildren(fragment);
      status.textContent = message;
      status.classList.toggle("warning", warning);
      popup.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    async function fetchCities(query, currentGeneration) {
      const requestController = new AbortController();
      controller = requestController;
      const timeout = setTimeout(() => requestController.abort(), 15000);
      try {
        const response = await fetch(`/api/cities?q=${encodeURIComponent(query)}`, {
          signal: requestController.signal, cache: "no-store", credentials: "same-origin",
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || "城市搜索暂时不可用");
        if (currentGeneration !== generation || document.activeElement !== input) return;
        const cities = mergeCities(data.cities);
        const fallback = matchingLocalCities(query);
        const displayed = cities.length ? cities : fallback;
        const message = data.warning
          ? `${data.warning}${fallback.length && !cities.length ? "；下方为本地匹配城市。" : ""}`
          : displayed.length ? `找到 ${displayed.length} 个城市或机场，请点击选择` : "暂未找到匹配地点，请换一个城市名、机场名或代码重试。";
        render(displayed, message, Boolean(data.warning));
      } catch (error) {
        if (currentGeneration !== generation || document.activeElement !== input) return;
        const fallback = matchingLocalCities(query);
        const reason = error.name === "AbortError" ? "城市搜索响应超时" : "暂时无法连接城市搜索";
        render(fallback, `${reason}。${fallback.length ? "下方为本地匹配结果，也可稍后重试。" : "请稍后重试，或清空输入选择常用城市。"}`, true);
      } finally {
        clearTimeout(timeout);
        if (controller === requestController) controller = null;
      }
    }

    function openSearch(query = input.value.trim(), immediate = false) {
      cancelSearch();
      const fallback = matchingLocalCities(query);
      render(fallback, query ? "正在搜索城市…" : "常用城市 · 输入中文、拼音或代码搜索更多");
      if (!query) return;
      const currentGeneration = generation;
      debounce = setTimeout(() => fetchCities(query, currentGeneration), immediate ? 0 : 250);
    }

    input.addEventListener("focus", () => {
      if (suppressFocusOpen) return;
      selectOnClick = true;
      if (input.value) input.select();
      const selected = selectedPlace(input) || cityFor(input.value);
      openSearch(selected ? selected.city_name || selected.city_code : input.value.trim());
    });
    input.addEventListener("click", () => {
      if (selectOnClick && input.value) input.select();
      selectOnClick = false;
      const selected = selectedPlace(input) || cityFor(input.value);
      if (popup.hidden) openSearch(selected ? selected.city_name || selected.city_code : input.value.trim());
    });
    input.addEventListener("input", () => {
      delete input.dataset.placeIdentity;
      syncClear();
      formError("");
      detectMarket();
      if (!composing) openSearch();
    });
    input.addEventListener("compositionstart", () => { composing = true; cancelSearch(); });
    input.addEventListener("compositionend", () => { composing = false; openSearch(); });
    input.addEventListener("keydown", (event) => {
      if (composing || event.isComposing) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const selected = selectedPlace(input) || cityFor(input.value);
        if (popup.hidden) openSearch(selected ? selected.city_name || selected.city_code : input.value.trim());
        if (options.length) markActive(event.key === "ArrowDown" ? (active + 1) % options.length : (active < 0 ? options.length - 1 : (active - 1 + options.length) % options.length));
      } else if (event.key === "Enter" && !popup.hidden) {
        event.preventDefault();
        if (active >= 0) choose(active);
        else {
          const exact = selectedPlace(input) || cityFor(input.value);
          const exactIndex = exact ? options.findIndex((city) => placeIdentity(city) === placeIdentity(exact)) : -1;
          if (exactIndex >= 0) choose(exactIndex);
          else if (exact) close();
          else if (options.length && input.value.trim()) choose(0);
        }
      } else if (event.key === "Escape") {
        if (!popup.hidden) event.preventDefault();
        close();
      } else if (event.key === "Tab") close();
    });
    wrapper.addEventListener("focusout", () => {
      // Touch browsers may focus the option or page before firing its click.
      setTimeout(() => { if (!wrapper.contains(document.activeElement)) close(); }, 180);
    });
    clear.addEventListener("click", () => {
      input.value = "";
      delete input.dataset.placeIdentity;
      syncClear();
      detectMarket();
      formError("");
      input.focus({ preventScroll: true });
      openSearch("");
    });
    document.addEventListener("pointerdown", (event) => { if (!wrapper.contains(event.target)) close(); });
    return { syncClear, close };
  }

  function detectMarket() {
    syncAirportSelectors();
    const origin = selectedPlace($("origin")) || cityFor($("origin").value);
    const destination = selectedPlace($("destination")) || cityFor($("destination").value);
    if ($("market").value !== "auto") {
      $("market-hint").textContent = "使用你选择的航线类型；也支持手动填写 3 位城市代码。";
    } else if (origin && destination) {
      const domestic = origin.market === "domestic" && destination.market === "domestic";
      $("market-hint").textContent = `已识别为${domestic ? "国内" : "国际 / 港澳台"}航线，也可手动调整。`;
    } else {
      $("market-hint").textContent = "搜索并选择城市后自动识别国内或国际航线。";
    }
  }

  function dateValue(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return NaN;
    return Date.parse(`${value}T00:00:00Z`);
  }

  function friendlyDate(value, weekday = false) {
    const time = dateValue(value);
    if (!Number.isFinite(time)) return String(value || "—");
    return new Intl.DateTimeFormat("zh-CN", {
      ...(state.bootstrap?.today?.slice(0, 4) !== value.slice(0, 4) ? { year: "numeric" } : {}),
      month: "long", day: "numeric", ...(weekday ? { weekday: "short" } : {}), timeZone: "UTC",
    }).format(new Date(time));
  }

  function friendlyTime(value, date = false) {
    if (!value) return "";
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      ...(date ? { month: "2-digit", day: "2-digit" } : {}),
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(parsed);
  }

  function updateDateHint() {
    const days = Math.round((dateValue($("end-date").value) - dateValue($("start-date").value)) / DAY) + 1;
    $("date-hint").textContent = Number.isFinite(days) && days > 0
      ? `比较 ${days} 个出发日期（包含首尾），不是往返日期。最多 31 天。`
      : "在这段时间内选择一天出发，最多比较 31 天。";
    if ($("start-date").value) $("end-date").min = $("start-date").value;
  }

  function updateMode() {
    const lowest = $("mode").value === "lowest";
    $("threshold").disabled = lowest;
    $("threshold").required = !lowest;
    $("mode-hint").textContent = {
      lowest: "定期发送所选平台的最低参考总价；未含税或不可比报价不参与。",
      threshold: "可比参考总价严格低于目标价时提醒，持续降价时也会提醒。",
      both: "可比参考总价严格低于目标价时提醒，并定期汇总本次查询最低价。",
    }[$("mode").value];
  }

  function providerName(id) {
    return state.bootstrap?.providers?.find((provider) => provider.id === id)?.name || id || "来源未标明";
  }

  function selectedProviders() {
    return Array.from($("provider-options").querySelectorAll('input[name="providers"]:checked'), (input) => input.value);
  }

  function updateProviderHint() {
    const count = selectedProviders().length;
    $("provider-selection-hint").textContent = count
      ? `已选择 ${count} 个平台。各平台支持范围和返回状态将在查询结果中列出。`
      : "请至少选择一个查询平台。";
    $("provider-selection-hint").classList.toggle("selection-error", !count);
  }

  function renderProviderOptions() {
    const fragment = document.createDocumentFragment();
    const groups = new Map();
    for (const [index, provider] of (state.bootstrap.providers || []).entries()) {
      const groupName = provider.group || "旅行平台";
      if (!groups.has(groupName)) {
        const group = element("div", "provider-group");
        group.setAttribute("role", "group");
        group.setAttribute("aria-label", groupName);
        group.append(element("p", "provider-group-title", groupName));
        groups.set(groupName, group);
        fragment.append(group);
      }
      const label = element("label", "provider-option");
      const checkbox = element("input");
      checkbox.type = "checkbox";
      checkbox.name = "providers";
      checkbox.value = provider.id;
      checkbox.id = `provider-option-${index}`;
      checkbox.checked = true;
      checkbox.addEventListener("change", () => { updateProviderHint(); formError(""); });
      label.htmlFor = checkbox.id;
      const text = element("span", "provider-option-text");
      text.append(element("span", "provider-option-name", provider.name || provider.id));
      if (provider.description) text.append(element("span", "provider-option-description", provider.description));
      label.append(checkbox, text);
      groups.get(groupName).append(label);
    }
    $("provider-options").replaceChildren(fragment);
    updateProviderHint();
  }

  function serverchanStatus() {
    return state.status?.serverchan || state.bootstrap?.serverchan || {
      configured: false, available: false, message: "尚未配置微信服务号推送密钥。", quota: null,
    };
  }

  function serverchanBinding() {
    return state.status?.serverchan_binding || state.bootstrap?.serverchan_binding || {
      state: "idle", message: "二维码仅在当前页面显示。绑定不会发送测试消息或启动监控。", qr_image: null,
    };
  }

  function safeBindingImage(value) {
    return typeof value === "string" && value.length <= 2000000
      && /^data:image\/(?:png|jpeg);base64,[A-Za-z0-9+/]+={0,2}$/.test(value) ? value : null;
  }

  function renderServerchanBinding() {
    const binding = serverchanBinding();
    const awaiting = binding.state === "waiting" || binding.state === "checking";
    const qr = awaiting ? safeBindingImage(binding.qr_image) : null;
    const image = $("serverchan-bind-qr");
    image.hidden = !qr;
    if (qr) {
      if (image.getAttribute("src") !== qr) image.setAttribute("src", qr);
    } else image.removeAttribute("src");
    $("serverchan-bind-message").textContent = binding.message || {
      idle: "二维码仅在当前页面显示。绑定不会发送测试消息或启动监控。",
      creating: "正在生成绑定二维码，请稍等。",
      waiting: "请用手机微信扫码，按提示关注或授权，再点击“我已扫码，完成绑定”。",
      checking: "正在检查你刚才的扫码授权，请稍等。",
      bound: "绑定已完成，推送凭证已保存到本机。可点击发送测试，在微信确认收到。",
      expired: "二维码已过期，请重新生成，或展开备用方式手动保存免费 SendKey。",
      error: "扫码绑定未完成，可重试或展开备用方式手动保存免费 SendKey。",
    }[binding.state] || "扫码绑定状态暂不可用，可展开备用方式。";
    if (binding.state === "waiting" && !qr) $("serverchan-bind-message").textContent = "绑定二维码无法显示，请取消后重新生成，或展开备用方式手动保存免费 SendKey。";
    $("serverchan-bind-message").classList.toggle("binding-error", ["expired", "error"].includes(binding.state) || (binding.state === "waiting" && !qr));
  }

  function updateChannel() {
    if (!state.bootstrap) return;
    const wechat = state.bootstrap.wechat;
    const isWechat = $("notify").value === "wechat";
    const isServerchan = $("notify").value === "serverchan";
    const serverchan = serverchanStatus();
    $("channel-note").classList.toggle("unavailable", (isWechat && !wechat.available) || (isServerchan && !serverchan.available));
    if (isServerchan) {
      $("channel-status").textContent = serverchan.available ? "微信服务号已配置 · 免费 5 次/天" : "微信服务号尚不可用";
      $("channel-description").textContent = serverchan.message || (serverchan.configured
        ? "通过 Server酱 Turbo 发送。无需登录桌面微信，发送是否收到请在微信确认。"
        : "用手机微信扫码完成一次绑定后，Linux 也可自动推送，无需手填密钥。绑定前仍可查询机票。");
    } else if (isWechat) {
      $("channel-status").textContent = wechat.available ? "微信环境可用 · 无需 Token" : "当前设备无法直接发送微信消息";
      $("channel-description").textContent = wechat.message || (wechat.available
        ? "向本机已登录微信的文件传输助手发送消息。请保持微信运行。"
        : "请在 Windows 电脑运行本程序并登录微信；当前设备可选择浏览器通知或配置微信服务号。");
    } else {
      $("channel-status").textContent = "浏览器提醒 · 无需 Token";
      $("channel-description").textContent = "开始监控时可授权桌面通知。请保持此页面打开；未授权时仍会在页面中显示提醒。";
    }
    $("test-wechat").hidden = !isWechat;
    $("serverchan-settings").hidden = !isServerchan;
    const quota = serverchan.quota;
    const hasQuota = quota && Number.isInteger(quota.used) && Number.isInteger(quota.remaining) && Number.isInteger(quota.limit);
    $("serverchan-quota").textContent = hasQuota
      ? `本程序 ${quota.today || "今天"} 已尝试 ${quota.used} 次，剩余 ${quota.remaining} / ${quota.limit} 次。`
      : "完成绑定后显示本程序当天用量。";
    $("serverchan-quota").classList.toggle("quota-exhausted", Boolean(hasQuota && quota.remaining <= 0));
    renderServerchanBinding();
    updateButtons();
  }

  function updateButtons() {
    if (!state.bootstrap) return;
    const busy = state.status?.search_busy || state.status?.monitor?.busy;
    const running = state.status?.monitor?.running;
    const serverchan = serverchanStatus();
    const binding = serverchanBinding();
    const bindingBusy = binding.state === "creating" || binding.state === "checking";
    const unavailableWechat = $("notify").value === "wechat" && !state.bootstrap.wechat.available;
    const unavailableServerchan = $("notify").value === "serverchan" && (!serverchan.configured || !serverchan.available);
    const credentialLocked = state.pending || Boolean(busy) || Boolean(running) || bindingBusy;
    const exhausted = serverchan.quota && Number.isInteger(serverchan.quota.remaining) && serverchan.quota.remaining <= 0;
    $("search-button").disabled = state.pending || Boolean(busy);
    $("search-label").textContent = busy ? "正在查询价格…" : "查询现在的最低价";
    $("start-button").disabled = state.pending || Boolean(busy) || Boolean(running) || unavailableWechat || unavailableServerchan;
    $("start-button").textContent = running ? "监控中，请先停止再修改" : "开始监控 →";
    $("start-button").title = unavailableWechat ? "当前设备微信不可用，请选择其他通知方式，或在 Windows 微信电脑运行。"
      : unavailableServerchan ? "请先完成微信服务号绑定；未绑定也可查询机票。" : "";
    $("stop-button").disabled = state.pending;
    $("test-wechat").disabled = credentialLocked || !state.bootstrap.wechat.available;
    $("serverchan-sendkey").disabled = credentialLocked;
    $("serverchan-save").disabled = credentialLocked || !$("serverchan-sendkey").value.trim();
    $("serverchan-clear").disabled = credentialLocked || !serverchan.configured;
    $("serverchan-test").disabled = credentialLocked || !serverchan.configured || !serverchan.available || exhausted;
    $("serverchan-bind-start").disabled = credentialLocked || binding.state === "waiting";
    $("serverchan-bind-confirm").disabled = credentialLocked || binding.state !== "waiting" || !safeBindingImage(binding.qr_image);
    $("serverchan-bind-cancel").disabled = credentialLocked || !["waiting", "expired", "error"].includes(binding.state);
    $("add-trip-button").disabled = state.pending || Boolean(busy) || Boolean(running) || state.trips.length >= MAX_TRIPS;
    for (const button of $("trip-list").querySelectorAll("button")) {
      button.disabled = state.pending || Boolean(busy) || Boolean(running);
    }
    $("serverchan-operation-hint").textContent = running
      ? "监控期间无法绑定、修改凭证或发送测试，请先停止监控。"
      : busy || state.pending || bindingBusy ? "当前操作完成后可绑定、修改凭证或发送测试。"
        : exhausted ? "本程序今天的 5 次发送额度已用完，明天可再发送。"
          : "绑定或保存密钥不会发送消息。发送测试后，请到微信服务号确认是否收到。";
  }

  function cloneTrip(trip) {
    return { ...trip, providers: [...trip.providers] };
  }

  function tripSignature(trip) {
    return JSON.stringify([
      trip.origin_scope, trip.origin, trip.origin_city_code,
      trip.destination_scope, trip.destination, trip.destination_city_code,
      trip.market, trip.start_date, trip.end_date,
      trip.mode, trip.threshold, ...trip.providers,
    ]);
  }

  function tripFromEditor() {
    formError("");
    if (!$("watch-form").reportValidity()) return null;
    let origin = normalizeCity($("origin"));
    let destination = normalizeCity($("destination"));
    const originCode = $("origin").value.trim().toUpperCase();
    const destinationCode = $("destination").value.trim().toUpperCase();
    if (!origin && /^[A-Z]{3}$/.test(originCode)) origin = {
      scope: "city", code: originCode, city_code: originCode,
      label: `${originCode}（全部机场）`, name: originCode,
    };
    if (!destination && /^[A-Z]{3}$/.test(destinationCode)) destination = {
      scope: "city", code: destinationCode, city_code: destinationCode,
      label: `${destinationCode}（全部机场）`, name: destinationCode,
    };
    if (!origin || !destination) throw new Error("请在出发地和目的地输入城市名称或拼音，再点击搜索结果选择城市。");
    const marketChoice = $("market").value;
    if (marketChoice === "auto" && (!origin.market || !destination.market)) throw new Error("输入了列表外城市代码，请手动选择国内或国际航线类型。");
    const market = marketChoice === "auto"
      ? (origin.market === "domestic" && destination.market === "domestic" ? "domestic" : "international")
      : marketChoice;
    if (origin.city_code === destination.city_code) throw new Error("出发地和目的地不能相同或属于同一城市。");
    const startDate = $("start-date").value;
    const endDate = $("end-date").value;
    const days = Math.round((dateValue(endDate) - dateValue(startDate)) / DAY) + 1;
    if (!Number.isFinite(days) || days < 1 || days > 31) throw new Error("请选择有效日期区间，最晚日期不能早于最早日期，且最多包含 31 天。");
    if (startDate < state.bootstrap.today || endDate > state.bootstrap.max_date) throw new Error(`可查询日期为 ${state.bootstrap.today} 至 ${state.bootstrap.max_date}。`);
    const threshold = $("mode").value === "lowest" ? null : Number($("threshold").value);
    if (threshold !== null && (!Number.isFinite(threshold) || threshold <= 0 || threshold > 1000000)) throw new Error("目标价格必须是 1 至 1000000 元之间的有效金额。");
    const providers = selectedProviders();
    if (!providers.length) throw new Error("请至少勾选一个查询平台。");
    return {
      origin: origin.code, destination: destination.code, market,
      origin_scope: origin.scope === "airport" ? "airport" : "city",
      destination_scope: destination.scope === "airport" ? "airport" : "city",
      origin_city_code: origin.city_code || origin.code,
      destination_city_code: destination.city_code || destination.code,
      origin_label: cityLabel(origin), destination_label: cityLabel(destination),
      start_date: startDate, end_date: endDate, threshold, mode: $("mode").value,
      providers,
    };
  }

  function globalSettingsFromForm() {
    const interval = Number($("interval").value);
    if (!Number.isInteger(interval) || interval < 10 || interval > 10080) throw new Error("查询间隔必须在 10 至 10080 分钟之间。");
    const notify = $("notify").value;
    if (!["wechat", "serverchan", "browser"].includes(notify)) throw new Error("请选择有效的提醒方式。");
    return { interval_minutes: interval, notify };
  }

  function settingsFromForm() {
    const globals = globalSettingsFromForm();
    const editor = tripFromEditor();
    if (!editor) return null;
    if (!state.trips.length) return { ...editor, ...globals };
    if (!state.trips.some((trip) => tripSignature(trip) === tripSignature(editor))) {
      throw new Error("当前表单有尚未加入清单的修改；请先添加当前行程，或载入清单中的行程再查询。");
    }
    return { trips: state.trips.map(cloneTrip), ...globals };
  }

  function applyTripToEditor(settings) {
    if (!settings) return;
    const fields = { market: "market", start_date: "start-date", end_date: "end-date", threshold: "threshold", mode: "mode" };
    for (const [key, id] of Object.entries(fields)) {
      if (settings[key] !== undefined && settings[key] !== null) $(id).value = String(settings[key]);
      else if (key === "threshold") $(id).value = "";
    }
    for (const side of ["origin", "destination"]) {
      const code = String(settings[side] || "").toUpperCase();
      const scope = settings[`${side}_scope`] === "airport" ? "airport" : "city";
      const cityCode = String(settings[`${side}_city_code`] || (scope === "city" ? code : "")).toUpperCase();
      const identity = `${scope}:${code}:${cityCode}`;
      let place = state.cityCatalog.get(identity);
      if (!place && /^[A-Z]{3}$/.test(code) && /^[A-Z]{3}$/.test(cityCode)) {
        const label = settings[`${side}_label`] || (scope === "city" ? `${code}（全部机场）` : `${code}（具体机场）`);
        place = { scope, code, city_code: cityCode, name: label, city_name: label,
          label, market: settings.market };
        state.cityCatalog.set(identity, place);
      }
      const input = $(side);
      if (place) {
        input.value = cityLabel(place);
        input.dataset.placeIdentity = identity;
      } else {
        input.value = code;
        delete input.dataset.placeIdentity;
      }
      state.cityPickers[side]?.syncClear();
    }
    const providers = Array.isArray(settings.providers)
      ? settings.providers
      : (state.bootstrap.providers || []).map((provider) => provider.id);
    for (const input of $("provider-options").querySelectorAll('input[name="providers"]')) input.checked = providers.includes(input.value);
    updateProviderHint();
    const origin = selectedPlace($("origin")) || cityFor($("origin").value);
    const destination = selectedPlace($("destination")) || cityFor($("destination").value);
    if (!settings.origin || !settings.destination) {
      $("market").value = "auto";
    } else if (origin && destination) {
      const detected = origin.market === "domestic" && destination.market === "domestic" ? "domestic" : "international";
      if (!settings.market || settings.market === detected) $("market").value = "auto";
    }
    detectMarket();
    updateDateHint();
    updateMode();
  }

  function renderTripList() {
    const list = $("trip-list");
    const fragment = document.createDocumentFragment();
    state.trips.forEach((trip, index) => {
      const item = element("li", "trip-list-item");
      const body = element("div", "trip-list-body");
      body.append(
        element("strong", "trip-list-route", `${index + 1}. ${trip.origin_label || cityName(trip.origin)} → ${trip.destination_label || cityName(trip.destination)}`),
        element("span", "trip-list-dates", `${friendlyDate(trip.start_date)} 至 ${friendlyDate(trip.end_date)} · ${trip.market === "domestic" ? "国内" : "国际 / 港澳台"}`),
        element("span", "trip-list-rule", trip.mode === "lowest" ? "最低价汇总" : `${trip.mode === "both" ? "最低价 + " : ""}低于 ¥${priceFormat.format(trip.threshold)}`),
        element("span", "trip-list-providers", `${trip.providers.length} 个平台：${trip.providers.map(providerName).join("、")}`),
      );
      const actions = element("div", "trip-list-actions");
      const load = element("button", "trip-list-button", "载入");
      load.type = "button";
      load.addEventListener("click", () => { applyTripToEditor(trip); formError(""); });
      const remove = element("button", "trip-list-button trip-list-remove", "删除");
      remove.type = "button";
      remove.addEventListener("click", () => {
        state.trips.splice(index, 1);
        if (state.trips.length) applyTripToEditor(state.trips[Math.min(index, state.trips.length - 1)]);
        renderTripList();
        formError("");
      });
      actions.append(load, remove);
      item.append(body, actions);
      fragment.append(item);
    });
    list.replaceChildren(fragment);
    $("trip-count").textContent = `${state.trips.length} / ${MAX_TRIPS}`;
    $("trip-list-hint").textContent = state.trips.length
      ? `查询和监控会覆盖清单中的 ${state.trips.length} 条行程。修改当前表单后，请再次点击添加。`
      : "可添加多条独立行程；清单为空时仍按当前表单查询一条行程。";
    updateButtons();
  }

  function addCurrentTrip() {
    try {
      const trip = tripFromEditor();
      if (!trip) return;
      if (state.trips.length >= MAX_TRIPS) throw new Error(`网页一次最多监控 ${MAX_TRIPS} 条行程。`);
      if (state.trips.some((item) => tripSignature(item) === tripSignature(trip))) throw new Error("这条行程已经在清单中。");
      state.trips.push(cloneTrip(trip));
      renderTripList();
      notice(`已添加 ${trip.origin_label} → ${trip.destination_label}，清单共 ${state.trips.length} 条行程。`);
    } catch (error) { formError(error.message); }
  }

  function applySettings(settings) {
    if (!settings) return;
    const savedTrips = Array.isArray(settings.trips) ? settings.trips : [];
    state.trips = savedTrips
      .filter((trip) => trip && typeof trip === "object" && Array.isArray(trip.providers))
      .slice(0, MAX_TRIPS).map(cloneTrip);
    applyTripToEditor(state.trips[0] || settings);
    for (const [key, id] of Object.entries({ interval_minutes: "interval", notify: "notify" })) {
      if (settings[key] === undefined || settings[key] === null) continue;
      if (id === "interval" && !Array.from($(id).options).some((option) => option.value === String(settings[key]))) {
        const option = document.createElement("option");
        option.value = String(settings[key]);
        option.textContent = `每 ${settings[key]} 分钟`;
        $(id).append(option);
      }
      $(id).value = String(settings[key]);
    }
    renderTripList();
    updateChannel();
  }

  async function action(path, settings, message) {
    if (state.pending) return;
    state.pending = true;
    updateButtons();
    try {
      await request(path, settings);
      notice(message);
      await poll();
    } catch (error) {
      notice(error.message, true);
    } finally {
      state.pending = false;
      updateButtons();
    }
  }

  async function startMonitor() {
    try {
      const settings = settingsFromForm();
      if (!settings) return;
      if (settings.notify === "wechat" && !state.bootstrap.wechat.available) throw new Error("当前设备无法发送微信消息，请选择浏览器通知，或在 Windows 电脑运行并登录微信。");
      if (settings.notify === "serverchan" && (!serverchanStatus().configured || !serverchanStatus().available)) throw new Error("请先在微信服务号区域完成扫码绑定，再开始监控；未绑定也可查询机票。");
      let permissionNote = "";
      if (settings.notify === "browser") {
        if ("Notification" in window && Notification.permission === "default") {
          try { await Notification.requestPermission(); } catch (_) { /* In-page reminders remain available. */ }
        }
        if (!("Notification" in window) || Notification.permission !== "granted") permissionNote = " 桌面通知未启用，提醒将显示在本页面。";
      }
      await action("/api/monitor/start", settings, `监控已启动，请保持本地程序运行。${permissionNote}`);
    } catch (error) { formError(error.message); }
  }

  function safeBookingUrl(value) {
    try {
      const url = new URL(value);
      const domains = ["ctrip.com", "ly.com", "qunar.com", "fliggy.com", "google.com", "kiwi.com", "ryanair.com", "trip.com", "skyscanner.com", "skyscanner.com.sg", "kayak.com", "momondo.com", "ch.com", "airasia.com"];
      if (url.protocol === "https:" && !url.username && !url.password && (!url.port || url.port === "443") && domains.some((domain) => url.hostname === domain || url.hostname.endsWith(`.${domain}`))) return url.href;
    } catch (_) { /* A missing or invalid link is omitted. */ }
    return null;
  }

  function element(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = String(content);
    return node;
  }

  function updateMonitor(status) {
    const monitor = status.monitor || {};
    const busy = status.search_busy || monitor.busy;
    const dot = $("monitor-dot");
    dot.className = `state-dot${busy ? " busy" : monitor.last_error ? " error" : monitor.running ? " running" : ""}`;
    $("monitor-heading").textContent = busy ? "正在查询航班参考价" : monitor.running ? "价格监控中" : "监控未运行";
    let detail = "选好行程后，可以立即查询或开始监控。";
    if (busy) {
      const sources = resultTrips(status.latest).flatMap((trip) => trip.sources || []);
      const completed = sources.filter((source) => source.status !== "pending").length;
      detail = sources.length ? `已完成 ${completed}/${sources.length} 项平台查询，报价陆续更新；完成比价后判断提醒。`
        : "正在获取所选日期的价格，报价会陆续显示。";
    }
    else if (monitor.running && monitor.settings) {
      const settings = monitor.settings;
      if (Array.isArray(settings.trips)) {
        detail = `${settings.trips.length} 条行程 · ${settings.interval_minutes} 分钟查询一次`;
      } else {
        detail = `${settings.origin_label || cityName(settings.origin)} → ${settings.destination_label || cityName(settings.destination)} · ${settings.interval_minutes} 分钟查询一次`;
      }
      if (monitor.next_run_at) detail += ` · 下次 ${friendlyTime(monitor.next_run_at, true)}`;
    }
    if (monitor.last_error && !busy) detail = `${detail} 最近一次：${monitor.last_error}`;
    $("monitor-detail").textContent = detail;
    $("stop-button").hidden = !monitor.running;
    const latest = status.latest;
    const trips = resultTrips(latest);
    const hasQuotes = trips.some((trip) => Array.isArray(trip.quotes) && trip.quotes.length);
    const allFailed = trips.length > 0 && trips.every((trip) => trip.error);
    const badge = $("result-badge");
    badge.textContent = busy ? "查询中" : allFailed || (latest?.error && !trips.length) ? "查询异常" : hasQuotes ? "已更新" : latest ? "暂无报价" : "等待查询";
    badge.className = `small-tag${busy ? " busy" : allFailed || (latest?.error && !trips.length) ? " error" : hasQuotes ? " ready" : ""}`;
  }

  function sourceSummary(trip, quotes, comparable) {
    const sources = Array.isArray(trip?.sources) ? trip.sources : [];
    const wrapper = element("div", "source-summary");
    wrapper.append(element("h4", "source-heading", "各平台查询结果"));
    const comparableProviders = new Set(comparable.map((quote) => quote.provider || quote.source).filter(Boolean));
    const count = comparableProviders.size;
    const scope = element("p", "comparison-scope", count >= 2
      ? `本次有${count}个平台返回可比报价，最低价仅针对所选平台及日期。`
      : count === 1
        ? "仅1个平台有可比报价；最低价仅代表该平台本次查询结果。"
        : "暂无可比的含税总价；平台返回状态及其他参考报价如下。");
    scope.classList.toggle("limited", count < 2);
    wrapper.append(scope);
    const statuses = { ok: "已返回报价", empty: "暂无报价", error: "查询失败", unsupported: "暂不支持", pending: "查询中" };
    const list = element("ul", "source-list");
    const fragment = document.createDocumentFragment();
    for (const source of sources) {
      const status = Object.hasOwn(statuses, source.status) ? source.status : "unknown";
      const card = element("li", `source-card source-${status}`);
      card.dataset.provider = source.id;
      const top = element("div", "source-card-top");
      const statusText = status === "unsupported" && String(source.message || "").includes("尚未接入")
        ? "本程序尚未接入机场筛选" : statuses[status] || "状态未返回";
      top.append(element("strong", "source-name", source.name || providerName(source.id)), element("span", "source-status", statusText));
      const matches = comparable.filter((quote) => quote.provider === source.id);
      const sourceQuotes = quotes.filter((quote) => quote.provider === source.id);
      const quoteCount = Number.isInteger(source.quote_count) && source.quote_count >= 0 ? source.quote_count : sourceQuotes.length;
      let detail = `${quoteCount} 条报价`;
      if (matches.length) detail += ` · 参考总价 ¥${priceFormat.format(Math.min(...matches.map((quote) => quote.price)))} 起`;
      else if (sourceQuotes.length) {
        const reference = sourceQuotes.reduce((lowest, quote) => quote.price < lowest.price ? quote : lowest);
        detail += ` · 参考展示 ¥${priceFormat.format(reference.price)} 起（不参与比价）`;
        if (typeof reference.original_price === "number" && Number.isFinite(reference.original_price)
            && /^[A-Z]{3}$/.test(reference.original_currency || "")) {
          detail += ` · ${reference.original_currency} ${priceFormat.format(reference.original_price)}`;
        }
      } else if (quoteCount) detail += " · 无可比总价";
      if (source.status === "pending") detail = "结果返回后自动更新";
      else if (Number.isFinite(source.elapsed_seconds)) detail += ` · 用时 ${source.elapsed_seconds} 秒`;
      card.append(top, element("p", "source-detail", detail));
      if (status === "unsupported" || status === "error") {
        const reason = element("p", "source-reason", String(source.message || "").split("；")[0]);
        reason.title = source.message || "";
        card.append(reason);
      }
      if (source.message) {
        const more = element("details", "source-more");
        more.append(element("summary", "source-more-summary", "查看查询详情"), element("p", "source-message", source.message));
        card.append(more);
      }
      const sourceUrl = safeBookingUrl(source.search_url);
      if (sourceUrl) {
        const link = element("a", "source-search-link", "打开平台查价 ↗");
        link.href = sourceUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        card.append(link);
      }
      fragment.append(card);
    }
    if (!sources.length && trip) fragment.append(element("li", "source-card source-unknown", "本次响应未提供各平台状态。"));
    list.append(fragment);
    wrapper.append(list);
    return wrapper;
  }

  function quoteBasis(quote) {
    if (quote.comparable !== false) return "含税参考总价";
    if (quote.price_basis === "base") return "未含税 · 不参与比价";
    if (quote.price_basis === "unknown") return "口径未确认 · 仅供参考";
    return "不可比报价 · 不参与比价";
  }

  function dailyLowestQuotes(quotes) {
    const winners = new Map();
    for (const quote of quotes) {
      const date = String(quote.departure_date || "");
      const current = winners.get(date);
      if (!current || quote.price < current.price) {
        winners.set(date, quote);
        continue;
      }
      if (quote.price !== current.price) continue;
      // 同价时优先保留能安全跳转的平台，再按平台名稳定选择，保证每天只有一行。
      const quoteHasLink = Boolean(safeBookingUrl(quote.url));
      const currentHasLink = Boolean(safeBookingUrl(current.url));
      const quoteLabel = `${providerName(quote.provider)}\u0000${quote.source || ""}`;
      const currentLabel = `${providerName(current.provider)}\u0000${current.source || ""}`;
      if ((quoteHasLink && !currentHasLink) || (quoteHasLink === currentHasLink && quoteLabel.localeCompare(currentLabel, "zh-CN") < 0)) {
        winners.set(date, quote);
      }
    }
    return Array.from(winners.values()).sort((a, b) => String(a.departure_date).localeCompare(String(b.departure_date)));
  }

  function resultTrips(latest) {
    if (Array.isArray(latest?.trips) && latest.trips.length) return latest.trips;
    return latest ? [latest] : [];
  }

  function validQuotes(trip) {
    return (Array.isArray(trip?.quotes) ? trip.quotes : [])
      .filter((quote) => typeof quote.price === "number" && Number.isFinite(quote.price) && quote.price > 0 && (!quote.currency || quote.currency === "CNY"))
      .sort((a, b) => String(a.departure_date).localeCompare(String(b.departure_date)) || Number(a.comparable === false) - Number(b.comparable === false) || a.price - b.price || String(a.source || a.provider).localeCompare(String(b.source || b.provider)));
  }

  function actualAirportText(quote) {
    const origin = typeof quote.origin_airport === "string" && /^[A-Z]{3}$/.test(quote.origin_airport) ? quote.origin_airport : "";
    const destination = typeof quote.destination_airport === "string" && /^[A-Z]{3}$/.test(quote.destination_airport) ? quote.destination_airport : "";
    return origin && destination ? `实际机场 ${origin} → ${destination}` : "";
  }

  function tripResultCard(trip, fallbackTime, index) {
    const quotes = validQuotes(trip);
    const comparable = quotes.filter((quote) => quote.comparable !== false);
    const dailyLowest = dailyLowestQuotes(comparable);
    const best = dailyLowest.length ? dailyLowest.reduce((lowest, quote) => quote.price < lowest.price ? quote : lowest) : null;
    const card = element("article", "trip-result-card");
    card.dataset.routeId = String(trip.route_id || "");
    const heading = element("div", "trip-result-heading");
    const title = element("div");
    title.append(
      element("p", "section-kicker", `TRIP ${index + 1}`),
      element("h3", "trip-result-title", trip.name || `${cityName(trip.origin)} → ${cityName(trip.destination)}`),
      element("p", "trip-result-range", `${friendlyDate(trip.start_date)} 至 ${friendlyDate(trip.end_date)} · ${trip.market === "domestic" ? "国内" : "国际 / 港澳台"}`),
    );
    heading.append(title, element("span", `small-tag${trip.in_progress ? " busy" : trip.error ? " error" : " ready"}`,
      trip.in_progress ? "查询中 · 报价更新中" : trip.error ? "部分或暂无结果" : "查询完成"));
    if (trip.in_progress) card.append(element("p", "notice", "正在比较其余平台，当前价格为已返回结果中的最低价，完整结果仍可能变化。"));
    card.append(heading, sourceSummary(trip, quotes, comparable));

    const startDate = trip.start_date || trip.settings?.start_date;
    const endDate = trip.end_date || trip.settings?.end_date;
    const rangeNote = startDate && endDate ? `查询区间：${friendlyDate(startDate)}至${friendlyDate(endDate)}。` : "";
    if (best) {
      const fare = element("div", "best-fare");
      const fareBody = element("div");
      const amount = element("div", "fare-amount");
      amount.append(element("span", "currency-symbol", "¥"), element("strong", "", priceFormat.format(best.price)), element("span", "price-unit", "起 / 人"));
      const tieDates = new Set(comparable.filter((quote) => quote.price === best.price).map((quote) => quote.departure_date));
      fareBody.append(
        element("p", "fare-route", `${trip.origin_label || cityName(trip.origin)} → ${trip.destination_label || cityName(trip.destination)} · ${trip.market === "domestic" ? "国内" : "国际 / 港澳台"}`),
        amount,
        element("p", "best-date", `${friendlyDate(best.departure_date, true)} 出发${tieDates.size > 1 ? ` · 共 ${tieDates.size} 天同价` : ""}`),
        element("p", "best-source", `最低价 App：${providerName(best.provider)} · 含税参考总价`),
      );
      const airports = actualAirportText(best);
      if (airports) fareBody.append(element("p", "best-airports", airports));
      const meta = element("div", "fare-meta");
      const threshold = trip.settings?.threshold;
      if (typeof threshold === "number" && threshold > 0) {
        meta.append(element("span", `target-badge${best.price >= threshold ? " above" : ""}`,
          best.price < threshold ? `低于目标 ¥${priceFormat.format(threshold)}` : `目标 ¥${priceFormat.format(threshold)}`));
      }
      const bestUrl = safeBookingUrl(best.url);
      if (bestUrl) {
        const link = element("a", "fare-link", `去${providerName(best.provider)}购买 ↗`);
        link.href = bestUrl;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.setAttribute("aria-label", `前往${providerName(best.provider)}购买 ${trip.name || "该行程"} 的机票`);
        meta.append(link);
      }
      fare.append(fareBody, meta);
      card.append(fare);
    } else {
      card.append(element("div", "notice no-comparable", trip.in_progress ? "正在等待可比报价，查询结果将陆续显示。"
        : "本行程暂无可比的含税参考总价。未含税或口径未确认的报价不会被选作最低价，请查看平台状态。"));
    }
    card.append(element("p", "price-note", rangeNote + (best
      ? "每个出发日期只展示所选平台中可比的最低含税参考总价；其他平台结果见上方状态，成交价以跳转后的预订页面为准。"
      : trip.in_progress ? "查询完成后按各行程设置判断提醒条件。"
        : "本次只有未含税或口径未确认的参考报价，不能据此判断哪个平台的含税总价最低。")));

    if (dailyLowest.length) {
      const section = element("div", "daily-lowest-section");
      const tableHeading = element("div", "table-heading");
      tableHeading.append(element("h4", "", "每天最便宜的 App"), element("span", "", `查询于 ${friendlyTime(trip.queried_at || fallbackTime, true)}`));
      section.append(tableHeading, element("p", "table-scroll-hint", "左右滑动表格可查看 App 和跳转入口。"));
      const scroll = element("div", "table-scroll");
      const table = element("table");
      const caption = element("caption", "sr-only", `${trip.name || "该行程"}每天最低含税参考总价、对应 App 和购买入口`);
      const head = element("thead");
      const headRow = element("tr");
      for (const [label, className] of [["出发日期", ""], ["当日最低参考总价", ""], ["最便宜的 App", ""], ["前往购买", "table-link-heading"]]) {
        const cell = element("th", className, label);
        cell.scope = "col";
        headRow.append(cell);
      }
      head.append(headRow);
      const body = element("tbody");
      for (const quote of dailyLowest) {
        const isBest = Boolean(best) && quote.price === best.price;
        const row = element("tr", isBest ? "best-row" : "");
        row.dataset.provider = quote.provider || "";
        const dateCell = element("td");
        const dateNode = element("div", "row-date");
        dateNode.append(element("span", "", friendlyDate(quote.departure_date, true)), element("span", "best-pill", isBest ? "全程最低" : "当日最低"));
        dateCell.append(dateNode);
        const priceCell = element("td", "quote-price");
        const basis = element("span", "quote-basis", quoteBasis(quote));
        if (quote.price_note) basis.title = quote.price_note;
        priceCell.append(element("span", "quote-amount", `¥${priceFormat.format(quote.price)}`), basis);
        if (typeof quote.original_price === "number" && Number.isFinite(quote.original_price) && /^[A-Z]{3}$/.test(quote.original_currency || "")) {
          const original = element("span", "quote-basis", `原价 ${quote.original_currency} ${priceFormat.format(quote.original_price)}`);
          if (quote.exchange_date && quote.exchange_rate) original.title = `${quote.exchange_date} 参考汇率：1 ${quote.original_currency} = ${quote.exchange_rate} CNY`;
          priceCell.append(original);
        }
        const appName = providerName(quote.provider);
        const sourceCell = element("td", "quote-source", appName);
        if (quote.source && quote.source !== appName) sourceCell.title = `数据来源：${quote.source}`;
        const airportText = actualAirportText(quote);
        if (airportText) sourceCell.append(element("span", "quote-basis", airportText));
        const samePriceApps = new Set(comparable
          .filter((item) => item.departure_date === quote.departure_date && item.price === quote.price)
          .map((item) => item.provider || item.source));
        if (samePriceApps.size > 1) sourceCell.append(element("span", "quote-basis", `另有 ${samePriceApps.size - 1} 个 App 同价`));
        const linkCell = element("td");
        const url = safeBookingUrl(quote.url);
        if (url) {
          const link = element("a", "", `去${appName}购买 ↗`);
          link.href = url;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.setAttribute("aria-label", `前往${appName}购买 ${quote.departure_date} 出发的机票`);
          linkCell.append(link);
        } else linkCell.textContent = "—";
        row.append(dateCell, priceCell, sourceCell, linkCell);
        body.append(row);
      }
      table.append(caption, head, body);
      scroll.append(table);
      section.append(scroll);
      card.append(section);
    }
    if (trip.error) card.append(element("div", "notice notice-error result-notice", trip.error));
    const warnings = Array.isArray(trip.warnings) ? trip.warnings : [];
    if (warnings.length) {
      const details = element("details", "result-warnings");
      details.append(element("summary", "", `查看该行程查询说明 · ${warnings.length} 条`));
      const items = element("div");
      for (const warning of warnings) items.append(element("p", "", warning));
      details.append(items);
      card.append(details);
    }
    return card;
  }

  function renderResults(latest) {
    const signature = JSON.stringify(latest);
    if (signature === state.resultSignature) return;
    state.resultSignature = signature;
    const trips = resultTrips(latest);
    const fragment = document.createDocumentFragment();
    trips.forEach((trip, index) => fragment.append(tripResultCard(trip, latest?.queried_at, index)));
    $("trip-results").replaceChildren(fragment);
    $("trip-results").hidden = !trips.length;
    $("results-empty").hidden = Boolean(trips.length);
    $("empty-title").textContent = latest ? "这些行程暂时没有可用结果" : "好价格，从一段行程开始";
    $("empty-copy").textContent = latest ? "可以调整平台、日期或城市后重新查询。平台报错或没有报价，不代表没有航班。" : "选择出发地、目的地和可出发日期，查询国内或国际航班的最低参考价。";
    const fatal = latest?.error && !trips.length ? latest.error : "";
    $("result-error").hidden = !fatal;
    $("result-error").textContent = fatal;
  }

  function renderNotifications(notifications) {
    const items = Array.isArray(notifications) ? notifications : [];
    for (const item of items) {
      const key = String(item.id ?? `${item.created_at}-${item.title}`);
      if (!state.seenNotifications.has(key)) {
        state.seenNotifications.add(key);
        if (state.initializedStatus && item.channel === "browser" && "Notification" in window && Notification.permission === "granted") {
          try {
            const notification = new Notification(item.title || "机票价格提醒", { body: item.content || "", tag: `flightwatch-${key}` });
            notification.onclick = () => { window.focus(); notification.close(); };
          } catch (_) { /* Browser limitations do not affect the on-page record. */ }
        }
      }
    }
    const signature = JSON.stringify(items);
    if (signature === state.notificationSignature) return;
    state.notificationSignature = signature;
    $("notification-count").textContent = String(items.length);
    $("notifications-empty").hidden = items.length > 0;
    const fragment = document.createDocumentFragment();
    const ordered = [...items].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
    for (const item of ordered) {
      const row = element("li", "notification-item");
      const icon = element("span", "notification-icon", "↘");
      icon.setAttribute("aria-hidden", "true");
      const body = element("div", "notification-body");
      const topline = element("div", "notification-topline");
      topline.append(element("h3", "notification-title", item.title || "机票价格提醒"));
      const time = element("time", "notification-time", friendlyTime(item.created_at, true));
      if (item.created_at) time.dateTime = item.created_at;
      topline.append(time);
      const channelLabel = {
        wechat: "微信文件传输助手",
        serverchan: "微信服务号 · 已受理，请在微信确认",
        browser: "浏览器提醒",
      }[item.channel] || "提醒记录";
      body.append(topline, element("p", "notification-content", item.content || ""), element("div", "notification-channel", channelLabel));
      row.append(icon, body);
      fragment.append(row);
    }
    $("notification-list").replaceChildren(fragment);
  }

  async function poll() {
    if (state.polling || !state.bootstrap) return;
    state.polling = true;
    try {
      const status = await request("/api/status");
      state.status = status;
      $("connection-error").hidden = true;
      if (!state.initializedStatus && status.monitor?.running && status.monitor.settings) applySettings(status.monitor.settings);
      updateMonitor(status);
      renderResults(status.latest);
      renderNotifications(status.notifications);
      state.initializedStatus = true;
      updateChannel();
    } catch (error) {
      $("connection-error").textContent = `${error.message} 页面会自动尝试重新连接。`;
      $("connection-error").hidden = false;
    } finally { state.polling = false; }
  }

  async function initialize() {
    try {
      const bootstrap = await request("/api/bootstrap");
      state.bootstrap = bootstrap;
      mergeCities(bootstrap.cities);
      renderProviderOptions();
      for (const id of ["start-date", "end-date"]) {
        $(id).min = bootstrap.today;
        $(id).max = bootstrap.max_date;
      }
      $("version").textContent = bootstrap.version ? `v${bootstrap.version}` : "";
      $("form-fields").disabled = false;
      applySettings(bootstrap.defaults);
      await poll();
      setInterval(poll, 1000);
    } catch (error) {
      $("connection-error").textContent = `${error.message} 请确认本地程序已启动，然后刷新页面。`;
      $("connection-error").hidden = false;
      $("channel-status").textContent = "本地服务尚未连接";
    }
  }

  for (const id of ["origin", "destination"]) {
    $(`${id}-airport`).addEventListener("change", (event) => {
      const place = state.cityCatalog.get(event.target.value);
      if (!place) return;
      $(id).value = cityLabel(place);
      $(id).dataset.placeIdentity = placeIdentity(place);
      state.cityPickers[id].close();
      state.cityPickers[id].syncClear();
      formError("");
      detectMarket();
    });
    $(`${id}-airport-retry`).addEventListener("click", () => {
      const place = selectedPlace($(id));
      if (!place) return;
      state.airportLoads.delete(place.city_code);
      syncAirportSelectors();
    });
  }

  $("watch-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const settings = settingsFromForm();
      if (settings) await action("/api/search", settings, "查询已提交，结果区会自动更新。");
    } catch (error) { formError(error.message); }
  });
  $("add-trip-button").addEventListener("click", addCurrentTrip);
  $("start-button").addEventListener("click", startMonitor);
  $("stop-button").addEventListener("click", () => action("/api/monitor/stop", {}, "已停止自动监控和后续提醒。"));
  $("test-wechat").addEventListener("click", () => action("/api/wechat/test", {}, "微信测试任务已提交；请检查本机微信文件传输助手及提醒记录。"));
  $("serverchan-bind-start").addEventListener("click", () => action("/api/serverchan/bind/start", {}, "绑定二维码生成任务已提交，请在下方查看二维码。"));
  $("serverchan-bind-confirm").addEventListener("click", () => action("/api/serverchan/bind/confirm", {}, "扫码授权检查任务已提交，请查看下方绑定结果。"));
  $("serverchan-bind-cancel").addEventListener("click", () => action("/api/serverchan/bind/cancel", {}, "已取消本次扫码绑定。"));
  async function saveServerchan() {
    if ($("serverchan-save").disabled) return;
    const sendkey = $("serverchan-sendkey").value.trim();
    // Keep credentials out of trip settings, browser storage and query URLs.
    $("serverchan-sendkey").value = "";
    await action("/api/serverchan/save", { sendkey }, "推送密钥已保存到本机，未发送消息。可点击发送测试，在微信确认绑定。");
  }
  $("serverchan-sendkey").addEventListener("input", updateButtons);
  $("serverchan-sendkey").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); saveServerchan(); }
  });
  $("serverchan-save").addEventListener("click", saveServerchan);
  $("serverchan-clear").addEventListener("click", () => {
    $("serverchan-sendkey").value = "";
    action("/api/serverchan/clear", {}, "已清除本机推送密钥；Server酱账号的绑定不受影响。");
  });
  $("serverchan-test").addEventListener("click", () => action("/api/serverchan/test", {}, "微信服务号测试任务已提交，占用 1 次本程序发送额度；请查看提醒记录并到微信确认收到。"));
  $("notify").addEventListener("change", updateChannel);
  $("mode").addEventListener("change", updateMode);
  $("market").addEventListener("change", detectMarket);
  for (const id of ["origin", "destination"]) {
    state.cityPickers[id] = createCityPicker(id);
    $(id).addEventListener("change", detectMarket);
  }
  $("swap-cities").addEventListener("click", () => {
    const previous = $("origin").value;
    const previousIdentity = $("origin").dataset.placeIdentity || "";
    $("origin").value = $("destination").value;
    const destinationIdentity = $("destination").dataset.placeIdentity || "";
    $("destination").value = previous;
    if (destinationIdentity) $("origin").dataset.placeIdentity = destinationIdentity;
    else delete $("origin").dataset.placeIdentity;
    if (previousIdentity) $("destination").dataset.placeIdentity = previousIdentity;
    else delete $("destination").dataset.placeIdentity;
    state.cityPickers.origin.syncClear();
    state.cityPickers.destination.syncClear();
    state.cityPickers.origin.close();
    state.cityPickers.destination.close();
    detectMarket();
  });
  $("start-date").addEventListener("change", updateDateHint);
  $("end-date").addEventListener("change", updateDateHint);
  initialize();
})();
