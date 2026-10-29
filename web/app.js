"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const base = new URL("./", document.baseURI);
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const MAX_LINES = 2400;
  const CACHE_BUDGET = 8 * 1024 * 1024;
  const windowCache = new Map();
  const FOLDER_NODE_LIMIT = 512;
  let cachedBytes = 0;
  const THEME_KEY = "logpresent.theme";
  const state = {
    authenticated: false, authEnabled: true, authEpoch: 0,
    config: null, files: [], selected: null, generation: null,
    lines: [], cursor: 0, olderCursor: 0, newerCursor: 0, hasMore: false, hasNewer: false,
    following: true, pending: 0, pendingGeneration: null,
    stream: null, connected: false, lastLogUpdate: null,
    selecting: false, selectionRequest: 0, loadingHistory: false, loadingForward: false,
    jumping: false, navigationRequest: 0, positionPercent: 100, jumpKind: "percent",
    retryTimer: null, scrollTimer: null, programmaticScroll: false,
    query: "", matches: [], matchIndex: -1, searchTimer: null,
    tab: "logs", inventorySource: null, inventoryPage: 1,
    inventoryTotal: 0, inventoryRequest: 0, inventoryQuery:"", inventorySearchTimer:null,
    inventoryPollTimer:null, inventorySearchStatus:null, lastAuthCheck: 0,
    folderPath: "",
    logExpanded: new Map(), logTreeSignature: "", folderNodes: new Map(),
    explorerExpanded: {logs:null,inventory:null}, explorerManual: {logs:false,inventory:false},
    navigateExpanded:null, navigatePreference:null,
    wrapPreference:null,
    layoutFrame:null, layoutAnchor:null, layoutObserver:null,
    announcementTimer:null, announcementController:null, announcementRequest:0,
    announcementFile:null, announcementRevision:null, announcementTitle:"", announcementOpen:new Map(),
  };
  const folderIcon = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 5h5l2 2h7v9H3Zm0 0V3h5l2 2h7v2"/></svg>';
  const documentIcon = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 2h6l4 4v12H5Zm6 0v5h4M8 11h4m-4 3h4"/></svg>';
  const chevronIcon = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7 4 6 6-6 6"/></svg>';

  function api(path, params) {
    const url = new URL(`api/${path}`, base);
    if (params) for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined) url.searchParams.set(key, String(value));
    }
    return url;
  }
  async function responseError(response) {
    let detail = "";
    try { const result = await response.json(); if (typeof result.detail === "string") detail = result.detail; } catch (_) {}
    const error = new Error(detail || `Request failed (${response.status})`);
    error.status = response.status;
    return error;
  }
  async function getJSON(path, params) {
    const response = await fetch(api(path, params), {cache: "no-store"});
    if (response.status === 401) showGate("Your session has ended. Enter the passphrase to continue.");
    if (!response.ok) throw await responseError(response);
    return response.json();
  }
  const fileEndpoint = (id, action) => `logs/${encodeURIComponent(id)}/${action}`;
  function normalizeWindow(result) {
    const first = Number.isInteger(result.start_line) ? result.start_line : null;
    return {...result, lines: (result.lines || []).map((line,index) => ({...line,
      line_number: Number.isInteger(line.line_number) ? line.line_number : (first == null ? null : first + index),
    }))};
  }
  function cacheKey(kind,params) {
    return JSON.stringify([state.selected?.id,state.generation,kind,params,
      kind === "window" && params.percent != null ? state.selected?.size_bytes : null]);
  }
  function cacheGet(key) {
    const entry = windowCache.get(key);
    if (!entry) return null;
    if(entry.payload.has_newer===false && Number(entry.payload.size_bytes)!==Number(state.selected?.size_bytes)) {
      cachedBytes-=entry.bytes; windowCache.delete(key); return null;
    }
    windowCache.delete(key); windowCache.set(key,entry);
    return {...entry.payload,lines:entry.payload.lines.map(line => ({...line}))};
  }
  function cachePut(key,payload) {
    if (!Array.isArray(payload.lines) || !payload.generation) return;
    const bytes = payload.lines.reduce((total,line) => total + String(line.text || "").length * 2 + 128,512);
    if (bytes > CACHE_BUDGET) return;
    if (windowCache.has(key)) cachedBytes -= windowCache.get(key).bytes;
    windowCache.delete(key);
    windowCache.set(key,{payload:{...payload,lines:payload.lines.map(line => ({...line}))},bytes}); cachedBytes += bytes;
    while (cachedBytes > CACHE_BUDGET || windowCache.size > 96) {
      const oldest = windowCache.keys().next().value;
      cachedBytes -= windowCache.get(oldest).bytes; windowCache.delete(oldest);
    }
  }
  function clearFileCache(fileId) {
    for(const [key,entry] of windowCache) {
      if(JSON.parse(key)[0]===fileId) {cachedBytes-=entry.bytes; windowCache.delete(key);}
    }
  }
  async function cachedWindow(kind,params) {
    const key = cacheKey(kind,params), cached = cacheGet(key);
    if (cached) return cached;
    const id = state.selected.id, generation = state.generation;
    const raw = await getJSON(fileEndpoint(id,kind),params);
    if (raw.status === "indexing") return raw;
    const result = normalizeWindow(raw);
    if (state.authenticated && state.selected?.id === id && result.generation === generation) cachePut(key,result);
    return result;
  }
  function setLoading(active,text = "Opening file…") {
    $("log-loading").hidden = !active;
    $("log-loading-text").textContent = text;
    $("jump-go").disabled = active;
  }
  function formatBytes(bytes) {
    if (!Number.isFinite(Number(bytes))) return "—";
    const value = Math.max(0, Number(bytes));
    if (value < 1024) return `${Math.round(value)} B`;
    const units = ["KiB", "MiB", "GiB", "TiB", "PiB"];
    const order = Math.min(Math.floor(Math.log(value) / Math.log(1024)) - 1, units.length - 1);
    return `${(value / (1024 ** (order + 1))).toFixed(2)} ${units[order]}`;
  }
  function formatGiB(bytes) {
    const configured = state.config?.display?.size_decimals;
    const decimals = Number.isInteger(configured) && configured >= 0 && configured <= 6 ? configured : 4;
    return Number.isFinite(Number(bytes)) ? `${(Math.max(0, Number(bytes)) / (1024 ** 3)).toFixed(decimals)} GiB` : "—";
  }
  function exactBytes(bytes) {
    return Number.isFinite(Number(bytes)) ? `${Number(bytes).toLocaleString()} bytes` : "";
  }
  function relativeTime(seconds) {
    if (!Number.isFinite(Number(seconds))) return "—";
    const age = Math.max(0, Math.floor(Date.now() / 1000 - Number(seconds)));
    if (age < 3) return "just now";
    if (age < 60) return `${age}s ago`;
    if (age < 3600) return `${Math.floor(age / 60)}m ago`;
    if (age < 86400) return `${Math.floor(age / 3600)}h ago`;
    return `${Math.floor(age / 86400)}d ago`;
  }
  function formatDate(value) {
    const date = new Date(typeof value === "number" ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString(undefined, {month:"short", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit"});
  }
  function notice(text = "") {
    $("viewer-notice").textContent = text;
    $("viewer-notice").hidden = !text;
  }
  function renderServerName() {
    const name = state.config?.display?.server_name || "";
    $("connection-server-name").textContent = name;
    $("connection-server").title = name ? `Server: ${name}` : "";
    $("connection-server").hidden = !name;
  }
  function connection(connected, label) {
    state.connected = connected;
    const mode = connected ? "live" : "warning";
    for (const id of ["connection-dot", "stream-dot"]) $(id).className = `status-dot ${mode}`;
    $("connection-label").textContent = label || (connected ? "Connected" : "Reconnecting");
    renderServerName();
    $("stream-status").textContent = label || (connected ? (state.following ? "Live" : "Paused") : "Reconnecting");
  }
  function updateTimes() {
    $("log-update-time").textContent = state.lastLogUpdate ? `Updated ${relativeTime(state.lastLogUpdate)}` : "Waiting for output";
    $("log-update-time").title = state.lastLogUpdate ? formatDate(state.lastLogUpdate) : "";
  }

  function savedTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (_) { return null; }
  }
  function setTheme(theme, persist = false) {
    const selected = theme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = selected;
    const dark = selected === "dark";
    $("theme-label").textContent = dark ? "Light" : "Dark";
    $("theme-toggle").setAttribute("aria-label", `Switch to ${dark ? "light" : "dark"} theme`);
    $("theme-icon").innerHTML = dark
      ? '<circle cx="10" cy="10" r="3.5"/><path d="M10 1v2m0 14v2M1 10h2m14 0h2M3.6 3.6 5 5m10 10 1.4 1.4M3.6 16.4 5 15M15 5l1.4-1.4"/>'
      : '<path d="M16.4 12.6A7 7 0 0 1 7.4 3.6a7 7 0 1 0 9 9Z"/>';
    document.querySelector('meta[name="theme-color"]').setAttribute("content", dark ? "#151515" : "#e9edf1");
    if (persist) { try { localStorage.setItem(THEME_KEY, selected); } catch (_) {} }
  }
  function toggleTheme() { setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true); }

  function stopStream() {
    if (state.stream) state.stream.close();
    state.stream = null;
  }
  function cancelScrollSuppression() {
    clearTimeout(state.scrollTimer);
    state.programmaticScroll = false;
  }
  function suppressScroll(duration = 80) {
    clearTimeout(state.scrollTimer);
    state.programmaticScroll = true;
    state.scrollTimer = setTimeout(() => { state.programmaticScroll = false; }, duration);
  }
  function stopSmoothScroll() {
    const viewport = $("log-viewport");
    viewport.scrollTo({top: viewport.scrollTop, behavior: "auto"});
    cancelScrollSuppression();
  }
  function compactViewport() {return matchMedia("(max-width: 720px), (max-height: 560px)").matches;}
  function queueLayoutUpdate(anchor=null) {
    if(!state.authenticated) return;
    suppressScroll(180);
    if(anchor && !state.layoutAnchor) state.layoutAnchor=anchor;
    if(state.layoutFrame!=null) return;
    state.layoutFrame=requestAnimationFrame(()=>{
      state.layoutFrame=null;
      if(!state.authenticated) {state.layoutAnchor=null; return;}
      const retained=state.layoutAnchor || captureAnchor(); state.layoutAnchor=null;
      suppressScroll(180);
      const panel=state.tab==="logs" ? $("logs-panel") : $("inventory-panel");
      const body=state.tab==="logs" ? panel.querySelector(".log-area") : panel.querySelector(".inventory-table-wrap");
      const bodyRect=body.getBoundingClientRect(),content=body.parentElement;
      const top=bodyRect.top-$("workspace").getBoundingClientRect().top;
      const contentStyle=getComputedStyle(content),siblings=[...content.children];
      const trailing=siblings.slice(siblings.indexOf(body)+1);
      // A flex child may overflow a temporarily short content window. Its
      // parent's bottom is then above the real footer, so measure that footer
      // directly rather than subtracting the two bottom coordinates.
      const bottomChrome=trailing.reduce((sum,child)=>sum+child.getBoundingClientRect().height,0)
        +(parseFloat(contentStyle.borderBottomWidth)||0)+(parseFloat(contentStyle.paddingBottom)||0);
      const padding=parseFloat(getComputedStyle(document.querySelector(".main-content")).paddingBottom)||0;
      const minimum=state.tab==="logs" ? (innerHeight<=500 ? 240 : (innerWidth<=720 && innerHeight>=780 ? 380 : 300)) : 250;
      const pixels=Math.ceil(top+bottomChrome+minimum+padding+2);
      const previous=parseFloat($("workspace").style.getPropertyValue("--workspace-min-height"))||0;
      if(Math.abs(previous-pixels)>1) $("workspace").style.setProperty("--workspace-min-height",`${pixels}px`);
      requestAnimationFrame(()=>{
        if(!state.authenticated || state.tab!=="logs") return;
        if(state.following) toBottom(); else restoreAnchor(retained);
      });
    });
  }
  function setExplorerExpanded(kind,expanded,{manual=false}={}) {
    const anchor=state.authenticated ? captureAnchor() : null;
    if(state.authenticated) suppressScroll(250);
    const prefix=kind==="logs" ? "log" : "folder";
    const region=$(`${prefix}-explorer-region`),button=$(`${prefix}-explorer-toggle`);
    const moveFocus=!expanded && region.contains(document.activeElement);
    state.explorerExpanded[kind]=expanded;
    if(manual) state.explorerManual[kind]=true;
    region.hidden=!expanded; button.setAttribute("aria-expanded",String(expanded));
    if(kind==="logs") $("selected-file-label").setAttribute("aria-expanded",String(expanded));
    if(moveFocus) button.focus({preventScroll:true});
    queueLayoutUpdate(anchor);
  }
  function applyResponsiveLayout() {
    if(state.authenticated) suppressScroll(250);
    const expanded=!compactViewport();
    for(const kind of ["logs","inventory"]) {
      if(!state.explorerManual[kind] || state.explorerExpanded[kind]==null) setExplorerExpanded(kind,true);
    }
    setNavigationExpanded(expanded || state.navigatePreference===true);
    setWrapLines(state.wrapPreference ?? matchMedia("(max-width: 720px)").matches);
    queueLayoutUpdate();
  }
  function setNavigationExpanded(expanded,{manual=false}={}) {
    const anchor=state.authenticated ? captureAnchor() : null;
    if(state.authenticated) suppressScroll(250);
    const region=$("navigate-region"),button=$("navigate-toggle");
    const moveFocus=!expanded && region.contains(document.activeElement);
    state.navigateExpanded=expanded;
    if(manual) state.navigatePreference=expanded;
    region.hidden=!expanded; button.setAttribute("aria-expanded",String(expanded));
    if(moveFocus) button.focus({preventScroll:true});
    queueLayoutUpdate(anchor);
  }
  function setWrapLines(enabled,{manual=false}={}) {
    const anchor=state.authenticated ? captureAnchor() : null;
    if(state.authenticated) suppressScroll(200);
    if(manual) state.wrapPreference=enabled;
    $("wrap-toggle").setAttribute("aria-pressed",String(enabled));
    $("logs-panel").classList.toggle("wrap-lines",enabled);
    queueLayoutUpdate(anchor);
  }
  function safeMarkdownURL(value) {
    const candidate=value.trim();
    if(!/^(https?:|mailto:)/i.test(candidate) || /[\u0000-\u0020\u007f]/.test(candidate)) return null;
    try {
      const url=new URL(candidate);
      return ["http:","https:","mailto:"].includes(url.protocol) ? url.href : null;
    } catch(_) {return null;}
  }
  function markdownInline(parent,value,depth=0) {
    if(depth>5) {parent.append(document.createTextNode(value)); return;}
    const text=value.slice(0,8192); let index=0,buffer="",attempts=0;
    const flush=()=>{if(buffer) {parent.append(document.createTextNode(buffer)); buffer="";}};
    while(index<text.length && attempts++<4096) {
      const char=text[index];
      if(char==="\\" && index+1<text.length) {buffer+=text[index+1]; index+=2; continue;}
      if(char==="`") {
        const end=text.indexOf("`",index+1);
        if(end>index+1) {
          flush(); const code=document.createElement("code"); code.textContent=text.slice(index+1,end); parent.append(code); index=end+1; continue;
        }
      }
      const marker=text.slice(index,index+2);
      if(marker==="**" || marker==="__") {
        const end=text.indexOf(marker,index+2);
        if(end>index+2) {
          flush(); const strong=document.createElement("strong"); markdownInline(strong,text.slice(index+2,end),depth+1); parent.append(strong); index=end+2; continue;
        }
      }
      if(char==="*" || char==="_") {
        const end=text.indexOf(char,index+1);
        if(end>index+1 && !/\s/.test(text[index+1])) {
          flush(); const em=document.createElement("em"); markdownInline(em,text.slice(index+1,end),depth+1); parent.append(em); index=end+1; continue;
        }
      }
      if(char==="[") {
        const labelEnd=text.indexOf("](",index+1),end=labelEnd<0 ? -1 : text.indexOf(")",labelEnd+2);
        if(labelEnd>index+1 && end>labelEnd+2) {
          flush(); const label=text.slice(index+1,labelEnd),raw=text.slice(labelEnd+2,end).trim();
          const parts=raw.match(/^(\S+?)(?:\s+"([^"]*)")?$/),href=parts ? safeMarkdownURL(parts[1]) : null;
          if(href) {
            const link=document.createElement("a"); link.href=href; link.target="_blank"; link.rel="noopener noreferrer";
            if(parts[2]) link.title=parts[2]; markdownInline(link,label,depth+1); parent.append(link);
          } else parent.append(document.createTextNode(text.slice(index,end+1)));
          index=end+1; continue;
        }
      }
      buffer+=char; index+=1;
    }
    buffer+=text.slice(index); flush();
    if(value.length>text.length) parent.append(document.createTextNode(value.slice(text.length)));
  }
  function renderMarkdown(parent,markdown) {
    const fragment=document.createDocumentFragment(),lines=String(markdown || "").replace(/\r\n?/g,"\n").split("\n");
    function blocks(target,input,depth=0) {
      let index=0,count=0;
      const startsBlock=line=>/^\s*$|^ {0,3}#{1,6}\s|^ {0,3}(?:`{3,}|~{3,})|^\s*>|^\s*(?:[-+*]|\d+[.)])\s+/.test(line);
      while(index<input.length && count++<512) {
        const line=input[index];
        if(!line.trim()) {index+=1; continue;}
        const fence=line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
        if(fence) {
          const content=[]; index+=1;
          while(index<input.length && !new RegExp(`^ {0,3}${fence[1][0]}{${fence[1].length},}\\s*$`).test(input[index])) content.push(input[index++]);
          if(index<input.length) index+=1;
          const pre=document.createElement("pre"),code=document.createElement("code"); code.textContent=content.join("\n"); pre.append(code); target.append(pre); continue;
        }
        const heading=line.match(/^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?$/);
        if(heading) {const element=document.createElement(`h${heading[1].length}`); markdownInline(element,heading[2]); target.append(element); index+=1; continue;}
        if(/^ {0,3}(?:([-*_])\s*){3,}$/.test(line)) {target.append(document.createElement("hr")); index+=1; continue;}
        if(/^\s*>/.test(line) && depth<5) {
          const content=[];
          while(index<input.length && /^\s*>/.test(input[index])) content.push(input[index++].replace(/^\s*>\s?/,""));
          const quote=document.createElement("blockquote"); blocks(quote,content,depth+1); target.append(quote); continue;
        }
        const item=line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
        if(item) {
          const ordered=Boolean(item[2]),list=document.createElement(ordered ? "ol" : "ul");
          if(ordered && Number(item[2])>1) list.start=Math.min(Number(item[2]),1000000);
          while(index<input.length) {
            const match=input[index].match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
            if(!match || Boolean(match[2])!==ordered) break;
            const li=document.createElement("li"); markdownInline(li,match[3]); list.append(li); index+=1;
          }
          target.append(list); continue;
        }
        const content=[line]; index+=1;
        while(index<input.length && !startsBlock(input[index])) content.push(input[index++]);
        const paragraph=document.createElement("p"); markdownInline(paragraph,content.join("\n")); target.append(paragraph);
      }
      if(index<input.length) {const rest=document.createElement("pre"); rest.textContent=input.slice(index).join("\n"); target.append(rest);}
    }
    blocks(fragment,lines); parent.replaceChildren(fragment);
  }
  function stopAnnouncement(clear=false) {
    clearTimeout(state.announcementTimer); state.announcementTimer=null;
    state.announcementRequest+=1; state.announcementController?.abort(); state.announcementController=null;
    if(clear) {
      state.announcementFile=null; state.announcementRevision=null; state.announcementTitle="";
      $("announcement-card").hidden=true; $("announcement-body").replaceChildren();
      $("announcement-title").textContent=""; $("announcement-summary").textContent="";
      $("announcement-toggle").setAttribute("aria-expanded","false"); $("announcement-body").hidden=true;
      $("announcement-action").textContent="Read note";
    }
  }
  function setAnnouncementExpanded(expanded,{manual=false}={}) {
    const anchor=state.authenticated ? captureAnchor() : null;
    if(state.authenticated) suppressScroll(250);
    $("announcement-toggle").setAttribute("aria-expanded",String(expanded));
    $("announcement-body").hidden=!expanded;
    $("announcement-action").textContent=expanded ? "Close note" : "Read note";
    if(manual && state.announcementFile) state.announcementOpen.set(state.announcementFile,expanded);
    queueLayoutUpdate(anchor);
  }
  function applyAnnouncement(payload) {
    const anchor=captureAnchor();
    suppressScroll(250);
    if(!payload.enabled || !payload.markdown) {
      $("announcement-card").hidden=true; $("announcement-body").replaceChildren();
      $("announcement-title").textContent=""; $("announcement-summary").textContent="";
      state.announcementRevision=null; state.announcementTitle=""; queueLayoutUpdate(anchor); return;
    }
    const title=payload.title || "About this workspace",fresh=state.announcementRevision!==payload.revision;
    if(fresh) {
      const top=$("announcement-body").scrollTop; renderMarkdown($("announcement-body"),payload.markdown); $("announcement-body").scrollTop=top;
      const summary=($("announcement-body").querySelector("p,li,blockquote") || $("announcement-body")).textContent.replace(/\s+/g," ").trim();
      $("announcement-summary").textContent=summary.length>180 ? `${summary.slice(0,177)}…` : summary;
      const expanded=state.announcementOpen.has(state.announcementFile)
        ? state.announcementOpen.get(state.announcementFile)
        : (!compactViewport() && innerHeight>=900 && payload.markdown.length<=420);
      setAnnouncementExpanded(expanded);
    }
    $("announcement-title").textContent=title; $("announcement-card").hidden=false;
    state.announcementRevision=payload.revision; state.announcementTitle=title;
    queueLayoutUpdate(anchor);
  }
  async function refreshAnnouncement() {
    clearTimeout(state.announcementTimer); state.announcementTimer=null;
    if(!state.authenticated || !state.announcementFile || document.hidden) return;
    state.announcementController?.abort();
    const controller=new AbortController(); state.announcementController=controller;
    const requestId=++state.announcementRequest,epoch=state.authEpoch,fileId=state.announcementFile;
    let refresh=15;
    try {
      const response=await fetch(api("announcement",{file_id:fileId}),{cache:"no-store",signal:controller.signal});
      if(!state.authenticated || epoch!==state.authEpoch || requestId!==state.announcementRequest || fileId!==state.announcementFile) return;
      if(response.status===401) {showGate("Your session has ended. Enter the passphrase to continue."); return;}
      if(!response.ok) throw await responseError(response);
      const payload=await response.json();
      if(!state.authenticated || epoch!==state.authEpoch || requestId!==state.announcementRequest || fileId!==state.announcementFile) return;
      refresh=Number(payload.refresh_seconds)||15; refresh=Math.min(3600,Math.max(1,refresh));
      applyAnnouncement(payload);
    } catch(error) {
      if(error.name!=="AbortError" && state.authenticated && requestId===state.announcementRequest && !state.announcementRevision) $("announcement-card").hidden=true;
    } finally {
      if(state.authenticated && epoch===state.authEpoch && requestId===state.announcementRequest && fileId===state.announcementFile) {
        state.announcementController=null;
        if(!document.hidden) state.announcementTimer=setTimeout(refreshAnnouncement,refresh*1000);
      }
    }
  }
  function startAnnouncement(fileId) {
    stopAnnouncement(true); state.announcementFile=fileId;
    queueLayoutUpdate(); refreshAnnouncement();
  }
  function clearWorkspace() {
    stopAnnouncement(true);
    if(state.layoutFrame!=null) cancelAnimationFrame(state.layoutFrame);
    state.layoutFrame=null; state.layoutAnchor=null;
    stopStream(); stopInventoryPolling(); clearTimeout(state.retryTimer); clearTimeout(state.searchTimer); clearTimeout(state.inventorySearchTimer); cancelScrollSuppression();
    state.selectionRequest += 1; state.inventoryRequest += 1; state.navigationRequest += 1;
    state.config = null; state.files = []; state.selected = null; state.generation = null;
    state.lines = []; state.cursor = 0; state.olderCursor = 0; state.newerCursor = 0; state.hasMore = false; state.hasNewer = false;
    state.selecting = false; state.loadingHistory = false; state.loadingForward = false; state.jumping = false; state.pending = 0;
    state.pendingGeneration = null; state.lastLogUpdate = null;
    state.query = ""; state.matches = []; state.matchIndex = -1;
    state.inventorySource = null; state.inventoryPage = 1; state.folderPath = ""; state.inventoryQuery="";
    state.inventorySearchStatus=null; $("inventory-search-status").hidden=true; $("inventory-search-status").textContent="";
    $("inventory-search").value="";
    windowCache.clear(); cachedBytes = 0;
    state.logExpanded.clear(); state.logTreeSignature=""; state.folderNodes.clear();
    state.announcementOpen.clear(); state.explorerManual={logs:false,inventory:false};
    state.navigateExpanded=null; state.navigatePreference=null; state.wrapPreference=null; applyResponsiveLayout();
    for (const id of ["log-lines", "log-explorer", "inventory-body", "folder-tree"]) $(id).replaceChildren();
    for (const id of ["file-path", "file-size", "inventory-patterns", "search-count", "history-status", "inventory-summary", "inventory-updated"]) $(id).textContent = "";
    $("file-path").title = ""; $("selected-file-label").title = ""; $("inventory-summary").title = "";
    $("selected-file-label").textContent = "Opening file…"; $("inventory-location").textContent = "Files"; $("inventory-location").title="";
    $("download-log").removeAttribute("href");
    $("jump-line").value = ""; $("jump-percent").value = "100"; $("jump-status").textContent = "Latest output";
    setLoading(false);
    $("inventory-patterns").title = "";
    $("log-search").value = ""; $("inventory-count").textContent = "—";
    $("loaded-lines").textContent = "0 lines loaded";
    $("log-explorer-count").textContent="";
    $("inventory-page-info").textContent = "Page 1";
    $("inventory-prev").disabled = true; $("inventory-next").disabled = true;
    $("brand-title").textContent = "AstroLOG"; $("project-title").textContent = "Workspace";
    document.title = "AstroLOG";
    notice(); setFollowing(true); connection(false, "Locked"); updateTimes();
  }
  function showGate(message = "Enter your access passphrase to open the workspace.") {
    state.authenticated = false; state.authEpoch += 1;
    clearWorkspace();
    $("workspace").hidden = true;
    $("placeholder-workspace").hidden = false;
    $("auth-gate").hidden = false;
    $("auth-token").value = "";
    $("auth-description").textContent = message;
    $("auth-submit").disabled = false;
    $("auth-submit").querySelector("span").textContent = "Open workspace";
  }
  async function checkSession() {
    if (!state.authEnabled || Date.now() - state.lastAuthCheck < 4000) return;
    state.lastAuthCheck = Date.now();
    try {
      const response = await fetch(api("auth/status"), {cache:"no-store"});
      if (response.ok) {
        const status = await response.json();
        if (!status.authenticated) showGate("Your session has ended. Enter the passphrase to continue.");
      }
    } catch (_) {}
  }
  async function openWorkspace(auth) {
    state.authEnabled = Boolean(auth.enabled);
    state.authenticated = true;
    const epoch = ++state.authEpoch;
    $("auth-error").hidden = true;
    $("auth-submit").disabled = true;
    $("auth-description").textContent = "Opening your workspace…";
    try {
      const [config, payload] = await Promise.all([getJSON("config"), getJSON("files")]);
      if (!state.authenticated || epoch !== state.authEpoch) return;
      applyConfig(config); state.files = payload.files || []; renderFiles();
      $("logout-button").hidden = !state.authEnabled;
      $("auth-divider").hidden = !state.authEnabled;
      $("auth-gate").hidden = true; $("placeholder-workspace").hidden = true;
      $("workspace").hidden = false;
      const first = state.files.find(file => file.id === config.display?.default_file) || state.files[0];
      if (first) await selectFile(first.id);
      else { connection(false,"No configured files"); notice("No log files are configured."); }
    } catch (error) {
      if (epoch !== state.authEpoch) return;
      showGate("The workspace is temporarily unavailable. Please try again.");
      $("auth-error").textContent = error.message; $("auth-error").hidden = false;
    }
  }
  async function login(event) {
    event.preventDefault();
    if (!$("auth-token").value) return;
    const body = JSON.stringify({token: $("auth-token").value});
    $("auth-token").value = "";
    $("auth-error").hidden = true; $("auth-submit").disabled = true;
    $("auth-submit").querySelector("span").textContent = "Opening…";
    try {
      const response = await fetch(api("auth/login"), {method:"POST", headers:{"Content-Type":"application/json"}, body, cache:"no-store"});
      if (!response.ok) {
        const error = await responseError(response);
        if (response.status === 401) error.message = "That passphrase didn’t match. Please try again.";
        if (response.status === 429) error.message = "Too many attempts. Please wait a minute and try again.";
        throw error;
      }
      await openWorkspace({enabled:true, authenticated:true});
    } catch (error) {
      $("auth-error").textContent = error.message; $("auth-error").hidden = false;
      $("auth-submit").disabled = false; $("auth-submit").querySelector("span").textContent = "Open workspace";
      $("auth-token").focus();
    }
  }
  async function logout() {
    $("logout-button").disabled = true;
    try {
      const response = await fetch(api("auth/logout"), {method:"POST", cache:"no-store"});
      if (!response.ok) throw await responseError(response);
      showGate(); $("auth-error").hidden = true;
    } catch (error) { notice(`Could not sign out: ${error.message}`); }
    finally { $("logout-button").disabled = false; }
  }

  function applyConfig(config) {
    state.config = config;
    const display = config.display || {};
    document.title = `${display.title || "AstroLOG"} · ${display.project || "Workspace"}`;
    $("brand-title").textContent = display.title || "AstroLOG";
    $("project-title").textContent = display.project || "Workspace";
    renderServerName();
    if (!savedTheme()) {
      const theme = display.theme === "auto" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : display.theme;
      setTheme(theme);
    }
  }
  function treeParts(label,icon,leaf=false) {
    const element=document.createElement("div"); element.className="tree-node";
    const row=document.createElement("div"); row.className="tree-row";
    const chevron=document.createElement(leaf ? "span" : "button"); chevron.className=`tree-chevron${leaf ? " is-leaf" : ""}`;
    chevron.innerHTML=chevronIcon;
    if(leaf) chevron.setAttribute("aria-hidden","true"); else chevron.type="button";
    const button=document.createElement("button"); button.type="button"; button.className="tree-label"; button.innerHTML=icon;
    const name=document.createElement("span"); name.className="tree-label-text"; name.textContent=label; button.append(name);
    row.append(chevron,button); element.append(row);
    const children=document.createElement("div"); children.className="tree-children";
    if(!leaf) element.append(children);
    return {element,row,chevron,button,name,children};
  }
  function captureTreeView(root) {
    const active=root.contains(document.activeElement) ? document.activeElement?.dataset.treeFocusKey : null;
    return {top:root.scrollTop,left:root.scrollLeft,active};
  }
  function restoreTreeView(root,view) {
    if(view.active) {
      const active=Array.from(root.querySelectorAll("[data-tree-focus-key]")).find(item=>item.dataset.treeFocusKey===view.active);
      active?.focus({preventScroll:true});
    }
    root.scrollTop=view.top; root.scrollLeft=view.left;
  }
  function renderFiles() {
    const explorer=$("log-explorer");
    $("log-explorer-count").textContent=String(state.files.length);
    const signature=JSON.stringify([(state.config?.sources || []).map(source=>[source.id,source.label]),state.files.map(file=>[file.id,file.source,file.relative_path,file.label])]);
    if(signature===state.logTreeSignature) {renderFolderSources(); markSelectedFile(); return;}
    const view=captureTreeView(explorer); state.logTreeSignature=signature;
    const fragment=document.createDocumentFragment();
    for(const source of state.config?.sources || []) {
      const tree={folders:new Map(),files:[],path:""};
      for(const file of state.files.filter(item=>item.source===source.id)) {
        const pieces=(file.relative_path || (file.path || "").split("/").pop() || file.id).split("/").filter(Boolean);
        let branch=tree;
        for(const [index,piece] of pieces.slice(0,-1).entries()) {
          if(!branch.folders.has(piece)) branch.folders.set(piece,{folders:new Map(),files:[],path:pieces.slice(0,index+1).join("/")});
          branch=branch.folders.get(piece);
        }
        branch.files.push({file,name:pieces[pieces.length-1]});
      }
      function folderBranch(branch,label,isSource=false) {
        const key=`${source.id}/${branch.path}`,parts=treeParts(label,folderIcon);
        if(isSource) parts.row.classList.add("is-source");
        if(!state.logExpanded.has(key)) state.logExpanded.set(key,true);
        parts.button.dataset.logFolderKey=key; parts.button.dataset.sourceId=source.id;
        parts.button.dataset.treeFocusKey=`log:${key}:label`; parts.chevron.dataset.treeFocusKey=`log:${key}:chevron`;
        function update() {
          const expanded=state.logExpanded.get(key)!==false;
          parts.children.hidden=!expanded; parts.chevron.setAttribute("aria-expanded",String(expanded));
          parts.chevron.setAttribute("aria-label",`${expanded ? "Collapse" : "Expand"} ${label}`);
        }
        function toggle() {state.logExpanded.set(key,state.logExpanded.get(key)===false); update();}
        parts.chevron.addEventListener("click",toggle); parts.button.addEventListener("click",toggle); update();
        for(const [name,child] of branch.folders) parts.children.append(folderBranch(child,name));
        for(const {file,name} of branch.files) {
          const leaf=treeParts(name,documentIcon,true); leaf.button.classList.add("log-file-entry"); leaf.button.dataset.fileId=file.id;
          leaf.button.dataset.treeFocusKey=`log:file:${file.id}`; leaf.button.title=file.label ? `${file.label} · ${file.relative_path || name}` : (file.relative_path || name);
          leaf.button.addEventListener("click",()=>selectFile(file.id)); parts.children.append(leaf.element);
        }
        return parts.element;
      }
      fragment.append(folderBranch(tree,source.label || source.id,true));
    }
    explorer.replaceChildren(fragment); restoreTreeView(explorer,view);
    renderFolderSources(); markSelectedFile();
  }
  function markSelectedFile() {
    if (!state.selected) return;
    for (const button of $("log-explorer").querySelectorAll("[data-file-id]")) {
      const active = button.dataset.fileId === state.selected.id;
      button.classList.toggle("active",active);
      button.closest(".tree-row")?.classList.toggle("active",active);
      if (active) button.setAttribute("aria-current","true"); else button.removeAttribute("aria-current");
    }
    $("selected-file-label").textContent = state.selected.relative_path || (state.selected.path || "").split("/").pop() || state.selected.label || state.selected.id;
    $("selected-file-label").title = state.selected.path || state.selected.label || state.selected.id;
    $("file-path").textContent = state.selected.path || "";
    $("file-size").textContent = formatBytes(state.selected.size_bytes);
    $("file-size").title = exactBytes(state.selected.size_bytes);
    $("download-log").href = api(fileEndpoint(state.selected.id,"download"));
  }
  async function refreshFiles() {
    if (!state.authenticated || !state.config) return;
    const epoch = state.authEpoch;
    try {
      const payload = await getJSON("files");
      if (!state.authenticated || epoch !== state.authEpoch) return;
      state.files = payload.files || [];
      if (state.selected) state.selected = state.files.find(file => file.id === state.selected.id) || state.selected;
      renderFiles();
    } catch (_) {}
  }
  function captureAnchor() {
    const viewport = $("log-viewport"), top = viewport.getBoundingClientRect().top;
    for (const row of $("log-lines").children) {
      const box = row.getBoundingClientRect();
      if (box.bottom > top + 4) return {key:row.dataset.start, top:box.top, viewportTop:top, scrollTop:viewport.scrollTop};
    }
    return {key:null, top:0, viewportTop:top, scrollTop:viewport.scrollTop};
  }
  function restoreAnchor(anchor) {
    if (!anchor) return;
    suppressScroll(100);
    const row = Array.from($("log-lines").children).find(element => element.dataset.start === anchor.key);
    const shift=$("log-viewport").getBoundingClientRect().top-(anchor.viewportTop ?? $("log-viewport").getBoundingClientRect().top);
    $("log-viewport").scrollTop = row ? $("log-viewport").scrollTop + row.getBoundingClientRect().top - anchor.top - shift : anchor.scrollTop;
  }
  function toBottom(smooth = false) {
    const animate = smooth && !reducedMotion;
    suppressScroll(animate ? 550 : 100);
    const viewport = $("log-viewport");
    viewport.scrollTo({top:viewport.scrollHeight, behavior:animate ? "smooth" : "auto"});
  }
  function visibleLogText(value) {
    return String(value ?? "").replace(/\r?\n$/,"")
      .replace(/\u001b\[[0-?]*[ -/]*[@-~]/g,"")
      .replace(/\u001b\][^\u0007]*?(?:\u0007|\u001b\\)/g,"");
  }
  function highlightedContent(parent,text,query) {
    const fullText=text;
    text=text.slice(0,16384);
    const tokenRegex = /\[[^\]\r\n]*\d{2}:\d{2}[^\]\r\n]*\]|\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?|\b(?:ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE|FATAL|CRITICAL|SUCCESS|NOTICE)\b|\b[A-Za-z_][\w.-]*(?==)|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?:%|(?:KiB|MiB|GiB|TiB|KB|MB|GB|TB|ms|s)\b)?/gi;
    const tokens = [], matches = [], boundaries = new Set([0,text.length]);
    for (const match of text.matchAll(tokenRegex)) {
      const word = match[0], upper = word.toUpperCase();
      let kind;
      if (/^\[|^\d{4}-\d{2}-\d{2}[T ]/.test(word)) kind = "token-timestamp";
      else if (/^(ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE|FATAL|CRITICAL|SUCCESS|NOTICE)$/.test(upper)) {
        const level = upper === "WARN" ? "warning" : upper.toLowerCase();
        kind = `token-level level-${level}`;
      } else kind = /^[A-Za-z_]/.test(word) ? "token-key" : "token-number";
      tokens.push({start:match.index,end:match.index+word.length,kind});
      boundaries.add(match.index); boundaries.add(match.index+word.length);
    }
    if (query) {
      const lower = text.toLocaleLowerCase(), needle = query.toLocaleLowerCase();
      for (let index=lower.indexOf(needle); index!==-1; index=lower.indexOf(needle,index+query.length)) {
        matches.push({start:index,end:index+query.length}); boundaries.add(index); boundaries.add(index+query.length);
      }
    }
    const edges = [...boundaries].sort((a,b) => a-b);
    let tokenIndex=0, matchIndex=0;
    for (let index=0; index<edges.length-1; index++) {
      const start=edges[index],end=edges[index+1]; if(start===end) continue;
      while(tokenIndex<tokens.length && tokens[tokenIndex].end<=start) tokenIndex++;
      while(matchIndex<matches.length && matches[matchIndex].end<=start) matchIndex++;
      const token=tokens[tokenIndex], matching=matches[matchIndex];
      let node=document.createTextNode(text.slice(start,end));
      if (token && token.start<=start && token.end>=end) {
        const span=document.createElement("span"); span.className=token.kind; span.append(node); node=span;
      }
      if (matching && matching.start<=start && matching.end>=end) {
        const mark=document.createElement("mark"); mark.append(node); node=mark;
      }
      parent.append(node);
    }
    if(fullText.length>text.length) parent.append(document.createTextNode(fullText.slice(text.length)));
  }
  function createLine(line,animate=false) {
    const row=document.createElement("div"); row.className="log-line"; row.dataset.start=String(line.start);
    if(animate) row.classList.add("new-line");
    const offset=document.createElement("span"); offset.className="line-offset";
    const numbered=Number.isInteger(line.line_number) && line.line_number>0;
    offset.textContent=numbered ? String(line.line_number) : `@${line.start}`;
    offset.dataset.kind=numbered ? "line" : "byte";
    offset.title=numbered ? `Line ${line.line_number} · byte ${line.start}` : `Byte offset ${line.start}; line index not yet available`;
    offset.setAttribute("aria-hidden","true");
    const content=document.createElement("span"); content.className="log-content";
    const text=visibleLogText(line.text);
    if(state.query && text.toLocaleLowerCase().includes(state.query.toLocaleLowerCase())) row.classList.add("search-match");
    highlightedContent(content,text,state.query);
    row.append(offset,content); return row;
  }
  function updateSearch() {
    state.matches = Array.from($("log-lines").querySelectorAll(".search-match"));
    state.matchIndex = Math.min(state.matchIndex,state.matches.length - 1);
    $("search-count").textContent = state.query ? `${state.matches.length} matching lines` : "";
    $("search-next").hidden = !state.query; $("search-next").disabled = !state.matches.length;
    $("search-shortcut").hidden = Boolean(state.query);
  }
  function renderAllLines() {
    const fragment = document.createDocumentFragment();
    for (const line of state.lines) fragment.append(createLine(line));
    $("log-lines").replaceChildren(fragment); updateLoadedState();
  }
  function animateWindow() {
    if(reducedMotion) return;
    const element=$("log-lines"); element.classList.remove("window-arrive");
    requestAnimationFrame(()=>{if(state.authenticated) element.classList.add("window-arrive");});
  }
  function updateLoadedState() {
    $("loaded-lines").textContent = `${state.lines.length.toLocaleString()} lines loaded`;
    $("log-empty").hidden = state.lines.length > 0;
    if (!state.lines.length && !state.selecting) {
      $("log-empty").querySelector("strong").textContent = state.selected?.exists === false ? "Waiting for the log file" : "Nothing written yet";
      $("log-empty").querySelector("span:last-child").textContent = "New lines will appear automatically.";
    }
    $("history-status").textContent = state.loadingHistory ? "Loading earlier lines…" : (state.loadingForward ? "Loading later lines…" : (state.lines.length && !state.hasMore ? "Beginning of file" : ""));
    if (!state.jumping) updatePositionStatus();
    updateSearch();
  }
  function updatePositionStatus() {
    const first=state.lines[0], last=state.lines[state.lines.length-1];
    const location=state.following ? "Latest" : `${Number(state.positionPercent || 0).toFixed(1)}%`;
    const range=first?.line_number && last?.line_number
      ? `lines ${first.line_number.toLocaleString()}–${last.line_number.toLocaleString()}`
      : "byte offsets";
    $("jump-status").textContent=state.lines.length ? `${location} · ${range}` : "No complete lines yet";
  }
  function setFollowing(value) {
    state.following = value;
    $("follow-toggle").classList.toggle("active",value);
    $("follow-toggle").setAttribute("aria-pressed",String(value));
    $("follow-label").textContent = value ? "Following" : "Resume";
    $("jump-latest").hidden = value;
    $("jump-label").textContent = state.pending > 0 ? `${state.pending.toLocaleString()} new lines · Back to latest` : "Back to latest";
    if (state.connected) $("stream-status").textContent = value ? "Live" : "Paused";
    if (!state.jumping) updatePositionStatus();
  }
  function parseEvent(event) { try { return JSON.parse(event.data); } catch (_) { return null; } }
  function openStream(requestId) {
    stopStream();
    if (!state.authenticated || !state.selected || state.generation == null) return;
    const stream = new EventSource(api(fileEndpoint(state.selected.id,"stream"),{cursor:state.cursor,generation:state.generation}));
    state.stream = stream;
    stream.onopen = () => { if (requestId === state.selectionRequest) connection(true); };
    stream.onerror = () => {
      if (requestId !== state.selectionRequest) return;
      connection(false,"Reconnecting"); checkSession();
    };
    stream.addEventListener("status",event => {
      if (!state.authenticated || requestId !== state.selectionRequest) return;
      const payload = parseEvent(event); if (!payload) return;
      connection(payload.connected !== false,payload.exists === false ? "Waiting for file" : undefined);
      if (payload.size_bytes != null) { state.selected.size_bytes = payload.size_bytes; markSelectedFile(); }
      if (payload.mtime != null) { state.lastLogUpdate = payload.mtime; updateTimes(); }
      if (payload.exists === false) notice("The configured file is not present yet. Waiting for output.");
      else if (!state.pendingGeneration) notice();
    });
    stream.addEventListener("append",event => {
      if (!state.authenticated || requestId !== state.selectionRequest) return;
      const payload = parseEvent(event); if (!payload || payload.file_id !== state.selected.id) return;
      if (payload.generation !== state.generation) { handleReset(payload,requestId); return; }
      const boundary = state.cursor;
      const lines = (payload.lines || []).filter(line => Number.isFinite(line.start) && Number.isFinite(line.end) && line.end > boundary && line.start >= boundary).map(line=>({...line}));
      state.cursor = Math.max(state.cursor,Number(payload.cursor) || 0,...lines.map(line => line.end));
      if (!lines.length) return;
      state.lastLogUpdate = Date.now()/1000; updateTimes();
      if (!state.following) { state.pending += lines.length; setFollowing(false); return; }
      const fragment = document.createDocumentFragment(), known = new Set(state.lines.map(line => line.start));
      let added = 0;
      for (const line of lines) {
        if (known.has(line.start)) continue;
        const previous=state.lines[state.lines.length-1];
        if (!Number.isInteger(line.line_number) && previous?.end===line.start && Number.isInteger(previous.line_number)) line.line_number=previous.line_number+1;
        known.add(line.start); state.lines.push(line); fragment.append(createLine(line,lines.length < 150)); added += 1;
      }
      state.newerCursor=state.lines[state.lines.length-1]?.end || state.cursor;
      state.hasNewer=false; state.positionPercent=100;
      $("log-lines").append(fragment);
      if (state.lines.length > MAX_LINES) {
        const count = state.lines.length - MAX_LINES; state.lines.splice(0,count);
        for (let index = 0; index < count; index++) $("log-lines").firstElementChild?.remove();
        state.olderCursor = state.lines[0]?.start ?? state.cursor; state.hasMore = state.olderCursor > 0;
      }
      updateLoadedState(); if (added) toBottom(added < 30);
    });
    stream.addEventListener("reset",event => {
      if (!state.authenticated || requestId !== state.selectionRequest) return;
      const payload = parseEvent(event); if (payload) handleReset(payload,requestId);
    });
    stream.addEventListener("error",event => {
      if (requestId !== state.selectionRequest || !event.data) return;
      const payload = parseEvent(event); if (payload?.detail) notice(`Live updates are unavailable: ${payload.detail}`);
    });
    stream.addEventListener("auth_expired",() => {
      if (requestId !== state.selectionRequest) return;
      showGate("Your session has ended. Enter the passphrase to continue.");
    });
  }
  function handleReset(payload,requestId) {
    stopStream(); clearFileCache(state.selected?.id); state.navigationRequest+=1; state.jumping=false; setLoading(false);
    state.loadingHistory=false; state.loadingForward=false;
    state.pendingGeneration = payload.generation || true;
    if (state.following) {
      notice("The file was restarted or replaced. Opening its latest contents…");
      loadTail(requestId).catch(error => {
        if (requestId !== state.selectionRequest || !state.authenticated) return;
        notice(error.status === 404 ? "Waiting for the replacement file…" : `Waiting to reopen the file: ${error.message}`);
        connection(false,"Waiting for file"); retryTail(requestId);
      });
    } else {
      notice("The file was restarted or replaced. Your view is preserved; resume to open the latest version.");
      setFollowing(false); $("jump-label").textContent = "File restarted · Open latest";
    }
  }
  async function loadTail(requestId = state.selectionRequest) {
    if (!state.authenticated || !state.selected) return;
    clearTimeout(state.retryTimer); stopStream();
    const id = state.selected.id, ticket=state.navigationRequest;
    const result = normalizeWindow(await getJSON(fileEndpoint(id,"tail"),{lines:state.config?.display?.initial_lines || 100}));
    if (!state.authenticated || requestId !== state.selectionRequest || state.selected?.id !== id || ticket!==state.navigationRequest) return;
    state.generation = result.generation; state.lines = result.lines || [];
    state.cursor = Number(result.cursor)||0; state.olderCursor = Number(result.older_cursor)||0;
    state.newerCursor=Number(result.newer_cursor ?? result.cursor)||0; state.hasNewer=Boolean(result.has_newer);
    state.hasMore = Boolean(result.has_more); state.pending = 0; state.pendingGeneration = null; state.positionPercent=100;
    state.selected.exists = true; state.selecting = false; state.loadingHistory = false; state.loadingForward=false; state.jumping=false;
    if (result.size_bytes != null) state.selected.size_bytes = result.size_bytes;
    if (result.mtime != null) state.lastLogUpdate = result.mtime;
    setFollowing(true); notice(); renderAllLines(); animateWindow(); markSelectedFile(); updateTimes();
    $("jump-line").value=""; $("jump-percent").value="100"; state.jumpKind="percent"; setLoading(false);
    requestAnimationFrame(() => { if(requestId === state.selectionRequest && state.authenticated) toBottom(); });
    openStream(requestId);
  }
  async function selectFile(id) {
    const file = state.files.find(item => item.id === id);
    if (!state.authenticated || !file) return;
    if(state.selected?.id===id && state.generation!=null) return;
    stopStream(); clearTimeout(state.retryTimer); cancelScrollSuppression();
    const requestId = ++state.selectionRequest; state.navigationRequest+=1;
    state.selected = file; state.selecting = true; state.generation = null; state.lines = [];
    state.cursor = 0; state.newerCursor=0; state.pending = 0; state.pendingGeneration = null; state.hasMore = false; state.hasNewer=false;
    state.loadingHistory=false; state.loadingForward=false; state.jumping=false;
    state.query = ""; state.matchIndex = -1; state.lastLogUpdate = file.mtime;
    if(!state.inventorySource) {state.inventorySource=file.source; state.inventoryPage=1; state.folderPath="";}
    $("log-search").value = ""; $("log-lines").replaceChildren();
    $("log-empty").hidden = false; $("log-empty").querySelector("strong").textContent = "Opening your log";
    $("log-empty").querySelector("span:last-child").textContent = "New lines will appear here.";
    setFollowing(true); notice(); markSelectedFile(); connection(false,"Connecting"); updateTimes();
    startAnnouncement(file.id);
    setLoading(true,"Opening file…"); renderFolderSources(); refreshFolders(); refreshInventory();
    try { await loadTail(requestId); }
    catch (error) {
      if (requestId !== state.selectionRequest || !state.authenticated) return;
      state.selecting = false; updateLoadedState();
      setLoading(false);
      if (error.status === 404) {
        state.selected.exists = false; notice("The configured file is not present yet. Waiting for output.");
        connection(false,"Waiting for file"); retryTail(requestId);
      } else { notice(`Unable to open this log: ${error.message}`); connection(false,"Unavailable"); }
    }
  }
  function retryTail(requestId) {
    clearTimeout(state.retryTimer);
    state.retryTimer = setTimeout(async () => {
      if (requestId !== state.selectionRequest || !state.authenticated) return;
      try { await loadTail(requestId); }
      catch (error) {
        if (requestId !== state.selectionRequest || !state.authenticated) return;
        if (error.status !== 404) notice(`Waiting to reopen the log: ${error.message}`);
        retryTail(requestId);
      }
    },3000);
  }
  async function loadHistory() {
    if (!state.authenticated || state.loadingHistory || state.loadingForward || state.jumping || !state.hasMore || !state.selected || state.pendingGeneration || state.selecting) return;
    const requestId = state.selectionRequest, generation = state.generation, ticket=state.navigationRequest;
    state.loadingHistory = true; updateLoadedState();
    try {
      const result = await cachedWindow("history",{before:state.olderCursor,lines:state.config?.display?.page_lines || 200,generation});
      if (!state.authenticated || requestId !== state.selectionRequest || generation !== state.generation || ticket!==state.navigationRequest) return;
      const anchor = captureAnchor(), known = new Set(state.lines.map(line => line.start));
      const older = (result.lines || []).filter(line => !known.has(line.start));
      state.lines = [...older,...state.lines];
      if (state.lines.length > MAX_LINES) {
        state.lines.length = MAX_LINES; state.newerCursor=state.lines[state.lines.length-1]?.end || 0; state.hasNewer=true;
        state.positionPercent=state.selected.size_bytes ? state.newerCursor/state.selected.size_bytes*100 : 0;
      }
      state.olderCursor = Number(result.older_cursor)||0; state.hasMore = Boolean(result.has_more) && older.length > 0;
      state.loadingHistory = false; renderAllLines(); restoreAnchor(anchor);
    } catch (error) {
      if (requestId !== state.selectionRequest || !state.authenticated || ticket!==state.navigationRequest) return;
      if (error.status === 409) {state.pendingGeneration = true; notice("The file changed. Resume live to open its current version.");}
      else notice(`Could not load earlier lines: ${error.message}`);
    } finally {
      if (requestId === state.selectionRequest && state.authenticated && ticket===state.navigationRequest) {state.loadingHistory = false; updateLoadedState();}
    }
  }
  async function resumeLive() {
    if (!state.authenticated || state.selecting || !state.selected) return;
    state.navigationRequest+=1; state.jumping=false;
    state.selecting = true; $("follow-label").textContent = "Opening…"; setLoading(true,"Opening latest output…");
    try { await loadTail(); }
    catch (error) {
      if (!state.authenticated) return;
      notice(`Unable to resume live: ${error.message}`); state.selecting = false; setFollowing(false); setLoading(false);
      if (error.status === 404) retryTail(state.selectionRequest);
    }
  }
  async function loadForward() {
    if (!state.authenticated || state.selecting || state.jumping || state.loadingForward || state.loadingHistory || !state.selected) return;
    if (!state.hasNewer) {resumeLive(); return;}
    const requestId=state.selectionRequest,generation=state.generation,ticket=state.navigationRequest;
    state.loadingForward=true; updateLoadedState();
    try {
      const result=await cachedWindow("forward",{after:state.newerCursor,generation,lines:state.config?.display?.page_lines || 200});
      if (!state.authenticated || requestId!==state.selectionRequest || generation!==state.generation || ticket!==state.navigationRequest) return;
      const anchor=captureAnchor(),known=new Set(state.lines.map(line=>line.start));
      const newer=result.lines.filter(line=>!known.has(line.start));
      state.lines.push(...newer);
      if(state.lines.length>MAX_LINES) state.lines.splice(0,state.lines.length-MAX_LINES);
      state.olderCursor=state.lines[0]?.start || 0; state.hasMore=state.olderCursor>0;
      state.newerCursor=Number(result.newer_cursor ?? result.cursor)||state.newerCursor;
      state.hasNewer=Boolean(result.has_newer); state.positionPercent=Number(result.position_percent)||0;
      state.loadingForward=false; renderAllLines(); restoreAnchor(anchor);
      if(!newer.length && !state.hasNewer) resumeLive();
    } catch(error) {
      if(requestId!==state.selectionRequest || !state.authenticated || ticket!==state.navigationRequest) return;
      if(error.status===409) {state.pendingGeneration=true; notice("The file changed. Open Latest to view the current file.");}
      else notice(`Could not load later lines: ${error.message}`);
    } finally {
      if(requestId===state.selectionRequest && state.authenticated && ticket===state.navigationRequest) {state.loadingForward=false; updateLoadedState();}
    }
  }
  async function jumpToPosition() {
    if(!state.authenticated || !state.selected || state.selecting) return;
    const exactLine=state.jumpKind==="line" && $("jump-line").value!=="";
    const value=Number(exactLine ? $("jump-line").value : $("jump-percent").value);
    if(!Number.isFinite(value) || (exactLine ? !Number.isInteger(value)||value<1 : value<0||value>100)) {
      notice(exactLine ? "Enter a whole line number starting at 1." : "Enter a position between 0 and 100%."); return;
    }
    if(!exactLine && value===100) {resumeLive(); return;}
    const params={lines:state.config?.display?.page_lines || 200}; params[exactLine ? "line" : "percent"]=value;
    const requestId=state.selectionRequest,ticket=++state.navigationRequest,generation=state.generation;
    const label=exactLine ? `line ${value.toLocaleString()}` : `${value}% of the file`;
    state.jumping=true; state.loadingHistory=false; state.loadingForward=false;
    setFollowing(false); stopSmoothScroll(); notice(); setLoading(true,`Opening ${label}…`);
    $("jump-status").textContent=`Opening ${label}…`;
    try {
      let result;
      while(true) {
        result=await cachedWindow("window",params);
        if(!state.authenticated || requestId!==state.selectionRequest || ticket!==state.navigationRequest) return;
        if(result.status!=="indexing") break;
        const progress=Number(result.index?.progress);
        const percent=Number.isFinite(progress) ? (progress<=1 ? progress*100 : progress).toFixed(0) : null;
        const text=`Indexing lines${percent==null ? "" : ` · ${percent}%`}…`;
        setLoading(true,text); $("jump-status").textContent=text;
        await new Promise(resolve=>setTimeout(resolve,450));
      }
      if(result.generation!==generation) {notice("The file changed while opening this position. Please open Latest."); return;}
      state.lines=result.lines; state.olderCursor=Number(result.older_cursor)||0; state.hasMore=Boolean(result.has_more);
      state.newerCursor=Number(result.newer_cursor ?? result.cursor)||0; state.hasNewer=Boolean(result.has_newer);
      state.positionPercent=Number(result.position_percent)||0; state.pending=0; state.pendingGeneration=null;
      state.jumping=false; renderAllLines(); animateWindow(); setFollowing(false);
      suppressScroll(120);
      const targetLine=exactLine ? state.lines.find(line=>line.line_number===value) : null;
      const targetOffset=targetLine?.start ?? result.target?.offset;
      const target=Array.from($("log-lines").children).find(row=>Number(row.dataset.start)>=Number(targetOffset));
      if(target) target.scrollIntoView({block:"center",behavior:"auto"}); else $("log-viewport").scrollTop=0;
    } catch(error) {
      if(requestId===state.selectionRequest && ticket===state.navigationRequest && state.authenticated) notice(`Could not open that position: ${error.message}`);
    } finally {
      if(requestId===state.selectionRequest && ticket===state.navigationRequest && state.authenticated) {
        state.jumping=false; setLoading(false); updatePositionStatus();
      }
    }
  }
  function setTab(tab) {
    if(tab!=="inventory") {stopInventoryPolling(); clearTimeout(state.inventorySearchTimer); state.inventorySearchTimer=null; state.inventoryRequest+=1;}
    state.tab = tab;
    for (const name of ["logs","inventory"]) {
      const active = name === tab;
      $(`${name}-tab`).classList.toggle("active",active);
      $(`${name}-tab`).setAttribute("aria-selected",String(active));
      $(`${name}-panel`).hidden = !active;
    }
    if (tab === "inventory") {refreshFolders(); refreshInventory();}
    else if (state.following) requestAnimationFrame(() => toBottom());
    queueLayoutUpdate();
  }
  const folderKey=(source,path)=>JSON.stringify([source,path || ""]);
  function createFolderNode(source,path,name,parentKey=null) {
    const key=folderKey(source,path),parts=treeParts(name,folderIcon);
    const node={key,source,path,label:name,parentKey,...parts,expanded:false,loaded:false,loading:false,
      childrenKeys:[],page:0,total:0,hasMore:false,error:"",request:0,touched:Date.now()};
    if(!path) node.row.classList.add("is-source");
    node.button.dataset.sourceId=source; node.button.dataset.folderPath=path;
    node.button.dataset.treeFocusKey=`folder:${key}:label`; node.chevron.dataset.treeFocusKey=`folder:${key}:chevron`;
    node.button.title=path || name;
    node.extra=document.createElement("div"); node.extra.className="tree-branch-status";
    node.status=document.createElement("div"); node.status.className="tree-loading";
    node.more=document.createElement("button"); node.more.type="button"; node.more.className="tree-more"; node.more.textContent="More folders…"; node.more.hidden=true;
    node.more.addEventListener("click",()=>loadFolderChildren(node,{more:true}));
    node.extra.append(node.status,node.more); node.children.append(node.extra);
    node.chevron.addEventListener("click",()=>{
      node.expanded=!node.expanded; node.touched=Date.now(); updateFolderNode(node);
      if(node.expanded) loadFolderChildren(node);
    });
    node.button.addEventListener("click",()=>chooseFolder(source,path));
    state.folderNodes.set(key,node); updateFolderNode(node); return node;
  }
  function updateFolderNode(node) {
    node.children.hidden=!node.expanded;
    node.chevron.setAttribute("aria-expanded",String(node.expanded));
    node.chevron.setAttribute("aria-label",`${node.expanded ? "Collapse" : "Expand"} ${node.label}`);
    const active=node.source===state.inventorySource && node.path===state.folderPath;
    node.row.classList.toggle("active",active);
    if(active) node.button.setAttribute("aria-current","page"); else node.button.removeAttribute("aria-current");
    node.status.className=node.loading ? "tree-loading" : "tree-empty";
    node.status.textContent=node.loading ? "Loading folders…" : (node.error || (node.loaded && !node.childrenKeys.length ? "No subfolders" : ""));
    node.status.hidden=!node.status.textContent;
    node.more.hidden=!node.hasMore; node.more.disabled=node.loading;
  }
  function clearFolderDescendants(node) {
    for(const key of node.childrenKeys) {
      const child=state.folderNodes.get(key); if(!child) continue;
      clearFolderDescendants(child); child.request+=1; child.element.remove(); state.folderNodes.delete(key);
    }
    node.childrenKeys=[]; node.loaded=false; node.page=0; node.hasMore=false;
    updateFolderNode(node);
  }
  function reserveFolderNodes(required,protectedNode) {
    const protectedKeys=new Set();
    for(const start of [protectedNode,state.folderNodes.get(folderKey(state.inventorySource,state.folderPath))]) {
      for(let node=start; node; node=state.folderNodes.get(node.parentKey)) protectedKeys.add(node.key);
    }
    const candidates=[...state.folderNodes.values()].filter(node=>!node.expanded && node.childrenKeys.length && !protectedKeys.has(node.key)).sort((a,b)=>a.touched-b.touched);
    while(state.folderNodes.size+required>FOLDER_NODE_LIMIT && candidates.length) clearFolderDescendants(candidates.shift());
    return state.folderNodes.size+required<=FOLDER_NODE_LIMIT;
  }
  async function loadFolderChildren(node,{more=false,force=false}={}) {
    if(!state.authenticated || node.loading || (node.loaded && !more && !force)) return;
    const epoch=state.authEpoch,ticket=++node.request,page=more ? node.page+1 : 1;
    node.loading=true; node.error=""; node.touched=Date.now(); updateFolderNode(node);
    try {
      const result=await getJSON("folders",{source:node.source,path:node.path,page,page_size:30});
      if(!state.authenticated || epoch!==state.authEpoch || state.folderNodes.get(node.key)!==node || ticket!==node.request) return;
      const incoming=result.folders || [];
      const missing=incoming.filter(folder=>!state.folderNodes.has(folderKey(node.source,folder.path))).length;
      if(!reserveFolderNodes(missing,node)) {
        node.error="Folder cache is full. Collapse an unused branch, then try again.";
        return;
      }
      for(const folder of incoming) {
        const key=folderKey(node.source,folder.path);
        const child=state.folderNodes.get(key) || createFolderNode(node.source,folder.path,folder.name,node.key);
        if(!node.childrenKeys.includes(key)) {
          node.childrenKeys.push(key); node.children.insertBefore(child.element,node.extra);
        }
      }
      node.loaded=true; node.page=Math.max(node.page,page); node.total=result.total_count || 0;
      node.hasMore=node.page*(result.page_size || 30)<node.total;
    } catch(error) {
      if(!state.authenticated || epoch!==state.authEpoch || state.folderNodes.get(node.key)!==node || ticket!==node.request) return;
      node.error=`Folders unavailable: ${error.message}`;
    } finally {
      if(state.authenticated && epoch===state.authEpoch && state.folderNodes.get(node.key)===node && ticket===node.request) {
        node.loading=false; updateFolderNode(node); updateInventoryEmpty();
      }
    }
  }
  function renderFolderSources() {
    const parent=$("folder-tree"),sources=state.config?.sources || [];
    const allowed=new Set(sources.map(source=>source.id));
    for(const node of [...state.folderNodes.values()]) if(!allowed.has(node.source) && !node.parentKey) {
      clearFolderDescendants(node); node.request+=1; node.element.remove(); state.folderNodes.delete(node.key);
    }
    const first=state.inventorySource || sources[0]?.id;
    for(const [index,source] of sources.entries()) {
      let node=state.folderNodes.get(folderKey(source.id,""));
      if(!node) {node=createFolderNode(source.id,"",source.label || source.id); node.expanded=source.id===first;}
      if(parent.children[index]!==node.element) parent.insertBefore(node.element,parent.children[index] || null);
      updateFolderNode(node);
      if(node.expanded && !node.loaded) loadFolderChildren(node);
    }
    markFolderSelection();
  }
  function markFolderSelection() {
    for(const node of state.folderNodes.values()) updateFolderNode(node);
    const source=state.config?.sources?.find(item=>item.id===state.inventorySource);
    $("inventory-location").textContent=state.inventoryQuery ? "Search results" : state.folderPath || source?.label || "Files";
    $("inventory-location").title=state.inventoryQuery ? "Filenames across all configured sources and subfolders" : state.folderPath ? `${source?.label || state.inventorySource} / ${state.folderPath}` : (source?.label || "");
  }
  function stopInventoryPolling() {clearTimeout(state.inventoryPollTimer); state.inventoryPollTimer=null;}
  function chooseFolder(source,path) {
    if(!state.authenticated || !state.config?.sources?.some(item=>item.id===source)) return;
    const node=state.folderNodes.get(folderKey(source,path)); if(!node) return;
    if(source!==state.inventorySource || (path || "")!==state.folderPath) {
      stopInventoryPolling();
      clearTimeout(state.inventorySearchTimer); state.inventorySearchTimer=null;
      state.inventoryQuery=""; $("inventory-search").value=""; state.inventoryRequest+=1;
      state.inventorySearchStatus=null; $("inventory-search-status").hidden=true;
    }
    state.inventorySource=source; state.folderPath=path || ""; state.inventoryPage=1;
    node.expanded=true; node.touched=Date.now(); markFolderSelection();
    loadFolderChildren(node); refreshInventory();
  }
  async function refreshFolders(force=false) {
    if(!state.authenticated || !state.inventorySource) return;
    renderFolderSources();
    const node=state.folderNodes.get(folderKey(state.inventorySource,state.folderPath));
    if(node && node.expanded) return loadFolderChildren(node,{force});
  }
  function updateInventoryEmpty() {
    const node=state.folderNodes.get(folderKey(state.inventorySource,state.folderPath));
    $("inventory-empty").textContent=state.inventoryQuery
      ? (state.inventorySearchStatus==="indexing" ? "Searching all sources and building the file list…"
        : state.inventorySearchStatus==="partial" ? "No matches in the files listed so far. The file list is incomplete."
        : `No filenames match “${state.inventoryQuery}” in the configured sources.`)
      : node?.childrenKeys.length
      ? "No matching files in this folder. Select a subfolder above."
      : "No matching files in this folder.";
  }
  function renderInventoryPatterns() {
    if(state.inventoryQuery) {
      $("inventory-patterns").textContent="All sources";
      $("inventory-patterns").title="Includes subfolders and files outside the folder-view patterns";
      return;
    }
    const source = state.config?.sources?.find(item => item.id === state.inventorySource);
    const patterns = source?.inventory?.patterns || source?.inventory_patterns || [];
    $("inventory-patterns").textContent = patterns.join(" · ");
    $("inventory-patterns").title = patterns.length ? `Configured patterns: ${patterns.join(", ")}` : "";
  }
  async function refreshInventory() {
    stopInventoryPolling();
    if (!state.authenticated || (!state.inventoryQuery && !state.inventorySource) || document.hidden || (state.inventoryQuery && state.tab!=="inventory")) return;
    const requestId=++state.inventoryRequest,epoch=state.authEpoch,source=state.inventorySource,path=state.folderPath;
    const query=state.inventoryQuery,page=state.inventoryPage;
    const current=()=>state.authenticated && epoch===state.authEpoch && requestId===state.inventoryRequest
      && source===state.inventorySource && path===state.folderPath && query===state.inventoryQuery && page===state.inventoryPage;
    let pollAgain=false;
    $("refresh-inventory").disabled = true; renderInventoryPatterns(); markFolderSelection();
    try {
      const endpoint=query ? api("search/files",{q:query,page,page_size:50}) : api("inventory",{source,path,page,page_size:50});
      const response=await fetch(endpoint,{cache:"no-store"});
      if(!current()) return;
      if(response.status===401) {showGate("Your session has ended. Enter the passphrase to continue."); return;}
      if(!response.ok) throw await responseError(response);
      const result=await response.json();
      if(!current()) return;
      state.inventorySearchStatus=query ? result.status || "ready" : null;
      const indexing=state.inventorySearchStatus==="indexing",partial=state.inventorySearchStatus==="partial";
      const totalPages=Math.max(1,Math.ceil(Number(result.total_count || 0)/(result.page_size || 50)));
      if(query && !indexing && page>totalPages) {state.inventoryPage=totalPages; return refreshInventory();}
      pollAgain=indexing;
      const status=$("inventory-search-status"); status.classList.toggle("is-indexing",indexing);
      const metadataWarning=!query && (result.stale || result.truncated);
      status.classList.toggle("is-partial",partial || metadataWarning); status.hidden=query ? (!indexing && !partial) : !metadataWarning;
      if(indexing) status.textContent=`Building the file list across all sources · ${Number(result.indexed_files || 0).toLocaleString()} files checked. Results are still updating.`;
      else if(partial) status.textContent=result.truncated ? "The file-list limit was reached. These search results are incomplete." : "Some files or folders could not be listed. These search results may be incomplete.";
      else if(metadataWarning) status.textContent=result.stale ? "Showing the last received file list. The remote metadata has not refreshed yet." : "The remote file-list limit was reached. This list is incomplete.";
      else status.textContent="";
      state.inventoryTotal = result.total_count || 0;
      $("inventory-count").textContent = indexing && !state.inventoryTotal ? "…" : state.inventoryTotal.toLocaleString();
      const count = document.createElement("strong");
      count.textContent=query ? (indexing && !state.inventoryTotal ? "Searching all sources…" : `${state.inventoryTotal.toLocaleString()} ${indexing ? "matches so far" : "matching files"}`) : `${state.inventoryTotal.toLocaleString()} files`;
      const total = document.createElement("strong"); total.className = "inventory-total"; total.textContent = formatGiB(result.total_bytes); total.title = exactBytes(result.total_bytes);
      $("inventory-summary").replaceChildren(count,document.createTextNode(" · "),total,document.createTextNode(" total"));
      $("inventory-summary").title = exactBytes(result.total_bytes);
      $("inventory-updated").textContent = result.updated_at ? `${result.origin==="manifest" ? "Received" : "Checked"} ${formatDate(result.updated_at)}` : "Metadata only";
      const fragment = document.createDocumentFragment();
      for (const file of result.files || []) {
        const row = document.createElement("tr"), nameCell = document.createElement("td");
        const wrap = document.createElement("div"); wrap.className = "inventory-file-name";
        const icon = document.createElement("span"); icon.className = "file-icon"; icon.setAttribute("aria-hidden","true");
        icon.innerHTML = file.is_dir
          ? '<svg viewBox="0 0 20 20"><path d="M3 5h5l2 2h7v9H3Zm0 0V3h5l2 2h7v2"/></svg>'
          : '<svg viewBox="0 0 20 20"><path d="M5 2h6l4 4v12H5Zm6 0v5h4M8 11h4m-4 3h4"/></svg>';
        const details=document.createElement("span"); details.className="inventory-entry-details";
        const registered=query && file.log_id && state.files.some(item=>item.id===file.log_id);
        const name = document.createElement(registered ? "button" : "span"); name.textContent = file.name; name.title = file.name;
        if(registered) {
          name.type="button"; name.className="inventory-result-link"; name.dataset.fileId=file.log_id;
          name.title=`Open ${file.name} in Logs`;
          name.addEventListener("click",()=>{if(!state.authenticated) return; setTab("logs"); selectFile(file.log_id);});
        }
        details.append(name);
        if(query) {
          row.classList.add("global-search-result");
          const location=document.createElement("span"); location.className="inventory-result-path";
          location.textContent=`${file.source_label || file.source} / ${file.path || file.name}`;
          location.title=location.textContent; details.append(location);
        }
        wrap.append(icon,details); nameCell.append(wrap);
        const size = document.createElement("td"); size.textContent = file.is_dir && !file.size_known ? "Directory" : formatGiB(file.size_bytes); size.title = file.is_dir && file.size_known ? `${exactBytes(file.size_bytes)} in the tracked files` : exactBytes(file.size_bytes);
        const modified = document.createElement("td"); modified.className = "modified-column"; modified.textContent = formatDate(file.mtime);
        row.append(nameCell,size,modified); fragment.append(row);
      }
      $("inventory-body").replaceChildren(fragment); $("inventory-empty").hidden = Boolean(result.files?.length);
      updateInventoryEmpty();
      $("inventory-page-info").textContent=indexing ? `Page ${result.page || state.inventoryPage} · searching…` : `Page ${result.page || state.inventoryPage} of ${totalPages}`;
      $("inventory-prev").disabled = state.inventoryPage <= 1; $("inventory-next").disabled = state.inventoryPage >= totalPages;
      queueLayoutUpdate();
    } catch (error) {
      if(!current()) return;
      $("inventory-summary").textContent = `Files unavailable: ${error.message}`; $("inventory-count").textContent = "—";
      if(query) {
        state.inventorySearchStatus="partial"; $("inventory-search-status").hidden=false;
        $("inventory-search-status").classList.remove("is-indexing"); $("inventory-search-status").classList.add("is-partial");
        $("inventory-search-status").textContent="The search could not finish. Use Refresh to try again.";
      }
    } finally {
      if(current()) {
        $("refresh-inventory").disabled = false;
        if(pollAgain && state.tab==="inventory" && !document.hidden) state.inventoryPollTimer=setTimeout(()=>{state.inventoryPollTimer=null; refreshInventory();},1000);
      }
    }
  }
  function nextMatch(direction = 1) {
    if (!state.matches.length) return;
    setFollowing(false); suppressScroll(reducedMotion ? 100 : 650);
    for (const row of state.matches) row.classList.remove("current-match");
    state.matchIndex = (state.matchIndex + direction + state.matches.length)%state.matches.length;
    const row = state.matches[state.matchIndex]; row.classList.add("current-match");
    row.scrollIntoView({block:"center",behavior:reducedMotion ? "auto" : "smooth"});
    $("search-count").textContent = `${state.matchIndex+1}/${state.matches.length} matching lines`;
  }
  function handleScroll() {
    if (!state.authenticated || state.programmaticScroll || state.selecting || state.jumping || state.tab !== "logs") return;
    const viewport = $("log-viewport");
    const distance = viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    if (distance > 45 && state.following) setFollowing(false);
    // Returning to the actual bottom is an explicit request to follow again.
    // Reopen a fresh tail because the SSE cursor kept advancing while paused.
    if (!state.following && distance <= 5 && !state.loadingHistory && !state.loadingForward) {
      if(state.hasNewer) loadForward(); else resumeLive();
      return;
    }
    if (!state.following && viewport.scrollTop < 110) loadHistory();
  }
  function bindEvents() {
    $("auth-form").addEventListener("submit",login);
    $("theme-toggle").addEventListener("click",toggleTheme);
    $("auth-theme-toggle").addEventListener("click",toggleTheme);
    $("logout-button").addEventListener("click",logout);
    $("log-explorer-toggle").addEventListener("click",()=>setExplorerExpanded("logs",!state.explorerExpanded.logs,{manual:true}));
    $("folder-explorer-toggle").addEventListener("click",()=>setExplorerExpanded("inventory",!state.explorerExpanded.inventory,{manual:true}));
    $("selected-file-label").addEventListener("click",()=>setExplorerExpanded("logs",true,{manual:true}));
    $("announcement-toggle").addEventListener("click",()=>setAnnouncementExpanded($("announcement-toggle").getAttribute("aria-expanded")!=="true",{manual:true}));
    $("navigate-toggle").addEventListener("click",()=>setNavigationExpanded(!state.navigateExpanded,{manual:true}));
    $("logs-tab").addEventListener("click",() => setTab("logs"));
    $("inventory-tab").addEventListener("click",() => setTab("inventory"));
    $("follow-toggle").addEventListener("click",() => {
      if (state.following) {stopSmoothScroll(); setFollowing(false);} else resumeLive();
    });
    $("jump-latest").addEventListener("click",resumeLive);
    $("jump-latest-button").addEventListener("click",resumeLive);
    $("jump-go").addEventListener("click",jumpToPosition);
    $("jump-line").addEventListener("input",()=>{state.jumpKind=$("jump-line").value==="" ? "percent" : "line";});
    $("jump-percent").addEventListener("input",()=>{state.jumpKind="percent"; $("jump-line").value="";});
    for(const id of ["jump-line","jump-percent"]) $(id).addEventListener("keydown",event=>{
      if(event.key==="Enter") {event.preventDefault(); jumpToPosition();}
    });
    $("download-log").addEventListener("click",event=>{if(!state.authenticated || !state.selected) event.preventDefault();});
    $("wrap-toggle").addEventListener("click",() => {
      setWrapLines($("wrap-toggle").getAttribute("aria-pressed")!=="true",{manual:true});
    });
    $("log-viewport").addEventListener("scroll",handleScroll,{passive:true});
    $("log-viewport").addEventListener("wheel",event => {
      stopSmoothScroll();
      if (event.deltaY < 0 && state.following) setFollowing(false);
      else if (event.deltaY > 0) requestAnimationFrame(handleScroll);
    },{passive:true});
    $("log-viewport").addEventListener("pointerdown",() => {
      stopSmoothScroll();
    },{passive:true});
    $("log-viewport").addEventListener("touchmove",() => {stopSmoothScroll(); if(state.following) setFollowing(false);},{passive:true});
    $("log-viewport").addEventListener("keydown",event => {
      if (["ArrowUp","PageUp","Home"].includes(event.key)) {stopSmoothScroll(); setFollowing(false);}
      if (["ArrowDown","PageDown","End"].includes(event.key)) {cancelScrollSuppression(); requestAnimationFrame(handleScroll);}
    });
    $("log-search").addEventListener("input",event => {
      clearTimeout(state.searchTimer); const value = event.target.value;
      state.searchTimer = setTimeout(() => {
        const anchor = captureAnchor(); state.query = value; state.matchIndex = -1;
        renderAllLines(); state.following ? toBottom() : restoreAnchor(anchor);
      },100);
    });
    $("log-search").addEventListener("keydown",event => {
      if (event.key === "Enter") {event.preventDefault(); nextMatch(event.shiftKey ? -1 : 1);}
      if (event.key === "Escape") {event.target.value=""; event.target.dispatchEvent(new Event("input")); event.target.blur();}
    });
    $("search-next").addEventListener("click",() => nextMatch());
    document.addEventListener("keydown",event => {
      if (!state.authenticated) return;
      if (event.key === "/" && !["INPUT","TEXTAREA","SELECT"].includes(document.activeElement?.tagName)) {
        event.preventDefault(); if(state.tab !== "logs") setTab("logs"); $("log-search").focus();
      }
    });
    $("inventory-search").addEventListener("input",event=>{
      stopInventoryPolling();
      clearTimeout(state.inventorySearchTimer);
      state.inventoryQuery=event.target.value.trim(); state.inventoryPage=1; state.inventoryRequest+=1;
      state.inventorySearchStatus=state.inventoryQuery ? "indexing" : null;
      $("inventory-search-status").hidden=!state.inventoryQuery;
      $("inventory-search-status").classList.toggle("is-indexing",Boolean(state.inventoryQuery));
      $("inventory-search-status").classList.remove("is-partial");
      $("inventory-search-status").textContent=state.inventoryQuery ? "Searching filenames across all configured sources…" : "";
      $("inventory-body").replaceChildren(); $("inventory-empty").hidden=false; updateInventoryEmpty();
      $("inventory-summary").textContent=state.inventoryQuery ? "Searching all sources…" : "Loading file metadata…";
      $("inventory-summary").title=""; $("inventory-count").textContent="…";
      markFolderSelection(); renderInventoryPatterns(); queueLayoutUpdate();
      $("inventory-prev").disabled=true; $("inventory-next").disabled=true;
      state.inventorySearchTimer=setTimeout(refreshInventory,200);
    });
    $("inventory-search").addEventListener("keydown",event=>{
      if(event.key==="Escape") {event.target.value=""; event.target.dispatchEvent(new Event("input"));}
      if(event.key==="Enter") {event.preventDefault(); clearTimeout(state.inventorySearchTimer); refreshInventory();}
    });
    $("refresh-inventory").addEventListener("click",()=>{clearTimeout(state.inventorySearchTimer); refreshInventory(); refreshFolders(true);});
    $("inventory-prev").addEventListener("click",() => {if(state.inventoryPage>1) {state.inventoryPage-=1; refreshInventory();}});
    $("inventory-next").addEventListener("click",() => {state.inventoryPage+=1; refreshInventory();});
    window.addEventListener("beforeunload",() => {stopStream(); clearTimeout(state.retryTimer); stopAnnouncement(); stopInventoryPolling();});
    window.addEventListener("resize",applyResponsiveLayout);
    document.addEventListener("visibilitychange",() => {
      if(document.hidden) {stopAnnouncement(); stopInventoryPolling();}
      else if(state.authenticated) {refreshFiles(); checkSession(); refreshAnnouncement(); if(state.tab==="inventory") refreshInventory();}
    });
    if(typeof ResizeObserver!=="undefined") {
      state.layoutObserver=new ResizeObserver(()=>queueLayoutUpdate());
      for(const element of document.querySelectorAll(".topbar,.workspace-navigation,.announcement-card,.explorer-section,.folder-section,.log-toolbar,.position-toolbar,.viewer-footer,.inventory-heading,.inventory-summary,.inventory-search-status,.inventory-footer")) state.layoutObserver.observe(element);
    }
  }
  async function initialize() {
    bindEvents(); applyResponsiveLayout(); setTheme(savedTheme() || "light"); updateTimes();
    try {
      const response = await fetch(api("auth/status"),{cache:"no-store"});
      if (!response.ok) throw await responseError(response);
      const auth = await response.json();
      state.authEnabled = Boolean(auth.enabled);
      if (auth.authenticated) await openWorkspace(auth);
      else showGate();
    } catch (error) {
      showGate("The workspace is temporarily unavailable. Please try again.");
      $("auth-error").textContent = error.message; $("auth-error").hidden = false;
    }
    setInterval(() => {if(state.authenticated) updateTimes();},1000);
    setInterval(refreshFiles,15000);
    setInterval(() => {if(state.authenticated && state.tab === "inventory") refreshInventory();},30000);
  }
  initialize();
})();
