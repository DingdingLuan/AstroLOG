/* AstroLOG public demo: synthetic data only. No backend or remote connections. */
"use strict";
(() => {
  const base = new URL("./", document.baseURI);
  const apiBase = new URL("api/", base);
  const nativeFetch = window.fetch.bind(window);
  const NativeEventSource = window.EventSource;
  const encoder = new TextEncoder();
  const sources = [
    {id:"observatory", label:"Observatory · demo", type:"local", inventory:{patterns:["*"], refresh_seconds:30}},
    {id:"pipeline", label:"Data pipeline · demo", type:"local", inventory:{patterns:["*"], refresh_seconds:30}},
  ];
  const specs = [
    {id:"observatory.run", source:"observatory", label:"Simulation output", path:"simulation/run.log", kind:"run"},
    {id:"observatory.cpu", source:"observatory", label:"CPU diagnostics", path:"diagnostics/cpu.log", kind:"cpu"},
    {id:"pipeline.transfer", source:"pipeline", label:"File transfer", path:"transfer/download.log", kind:"transfer"},
  ];
  const logs = new Map();
  const streams = new Set();
  let ticker = null;

  function textFor(spec, n) {
    const stamp = new Date(Date.UTC(2026,0,15,9,0,n * 2)).toISOString().replace(".000", "");
    const time = `[${stamp}]`;
    if (n % 47 === 0) return `${time} WARNING synthetic checkpoint: waiting for the next output batch\n`;
    if (n % 61 === 0) return `${time} SUCCESS checkpoint saved · demo data only\n`;
    if (spec.kind === "cpu") return `${time} INFO step=${n} gravity=${(0.7+(n%13)*0.037).toFixed(3)}s hydro=${(0.3+(n%17)*0.021).toFixed(3)}s cpu_usage=${(78+(n%19)).toFixed(1)}% memory=${(12+(n%11)/10).toFixed(2)}GiB\n`;
    if (spec.kind === "transfer") return `${time} INFO FILE snap_${String(n%60).padStart(3,"0")}.hdf5 progress=${(n*7%100).toFixed(1)}% received=${(n%9/10).toFixed(2)}GiB rate=${(42+n%31).toFixed(1)}MiB/s [synthetic]\n`;
    return `${time} INFO step=${n} scale_factor=${(0.02+n/40000).toFixed(6)} active_particles=${(1248000+n*173)%4000000} elapsed=${(n*1.28).toFixed(2)}s energy_error=${((n%17+1)*1e-7).toExponential(2)}\n`;
  }
  function append(log) {
    const text=textFor(log.spec,log.sequence++),start=log.size;
    log.size += encoder.encode(text).length;
    log.lines.push({text,start,end:log.size,line_number:log.lines.length+1});
    log.mtime=Date.now()/1000;
  }
  for (const spec of specs) {
    const log={spec,lines:[],size:0,sequence:1,generation:`demo-${spec.id}-1`,revision:1,mtime:Date.now()/1000};
    for(let n=0;n<1200;n++) append(log);
    logs.set(spec.id,log);
  }
  function fileInfo(log) {
    return {id:log.spec.id,source:log.spec.source,label:log.spec.label,relative_path:log.spec.path,path:log.spec.path,exists:true,size_bytes:log.size,mtime:log.mtime,generation:log.generation};
  }
  function number(params,key,fallback) {
    const value=Number(params.get(key));
    return params.has(key) && Number.isFinite(value) ? value : fallback;
  }
  function windowOf(log,begin,end,target=null) {
    const lines=log.lines.slice(Math.max(0,begin),Math.min(log.lines.length,end));
    const older=lines[0]?.start ?? log.size,newer=lines.at(-1)?.end ?? log.size;
    return {file_id:log.spec.id,generation:log.generation,lines,cursor:newer,older_cursor:older,newer_cursor:newer,has_more:older>0,has_newer:newer<log.size,size_bytes:log.size,mtime:log.mtime,start_line:lines[0]?.line_number ?? 1,position_percent:log.size ? newer/log.size*100 : 100,status:"ready",...(target ? {target} : {})};
  }
  function inventoryFiles() {
    const rows=[...logs.values()].map(log=>({name:log.spec.path.split("/").at(-1),path:log.spec.path,source:log.spec.source,source_label:sources.find(source=>source.id===log.spec.source).label,size_bytes:log.size,mtime:log.mtime,is_dir:false,log_id:log.spec.id}));
    for(let n=0;n<60;n++) rows.push({name:`snap_${String(n).padStart(3,"0")}.hdf5`,path:`snapshots/snap_${String(n).padStart(3,"0")}.hdf5`,source:"observatory",source_label:sources[0].label,size_bytes:Math.round((1+n/120)*1024**3),mtime:Date.now()/1000-(60-n)*60,is_dir:false,log_id:null});
    rows.push({name:"README.txt",path:"README.txt",source:"pipeline",source_label:sources[1].label,size_bytes:192,mtime:Date.now()/1000,is_dir:false,log_id:null});
    return rows;
  }
  function paginated(rows,params) {
    const page=Math.max(1,number(params,"page",1)),pageSize=Math.min(200,Math.max(1,number(params,"page_size",50)));
    return {files:rows.slice((page-1)*pageSize,page*pageSize),total_count:rows.length,total_bytes:rows.reduce((sum,row)=>sum+row.size_bytes,0),page,page_size:pageSize,updated_at:new Date().toISOString()};
  }
  function answer(path,params) {
    if(path==="auth/status" || path==="auth/login" || path==="auth/logout") return {enabled:false,authenticated:true};
    if(path==="config") return {display:{title:"AstroLOG",project:"Public demo · synthetic",server_name:"demo",theme:"auto",default_file:"observatory.run",initial_lines:100,page_lines:200,size_decimals:4},sources};
    if(path==="files") return {files:[...logs.values()].map(fileInfo)};
    if(path==="announcement") return {enabled:true,title:"Interactive demo · synthetic data only",revision:"static-demo-v1",refresh_seconds:3600,markdown:"This public demo runs entirely in your browser. Logs and file sizes are **synthetic**; no server or private data is connected. Try switching logs, jumping to a line, searching files, or changing the theme. [Get AstroLOG on GitHub](https://github.com/Dingdingluan/AstroLOG)."};
    if(path==="search/files") {
      const query=(params.get("q") || "").trim(),all=inventoryFiles();
      const rows=all.filter(row=>row.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
      return {...paginated(rows,params),query,status:"ready",indexed_files:all.length,truncated:false,skipped:{}};
    }
    if(path==="inventory") {
      const folder=params.get("path") || "",prefix=folder ? `${folder}/` : "";
      const rows=inventoryFiles().filter(row=>row.source===params.get("source") && row.path.startsWith(prefix) && !row.path.slice(prefix.length).includes("/"));
      return paginated(rows,params);
    }
    if(path==="folders") {
      const folder=params.get("path") || "",prefix=folder ? `${folder}/` : "";
      const names=new Set(inventoryFiles().filter(row=>row.source===params.get("source") && row.path.startsWith(prefix)).map(row=>row.path.slice(prefix.length)).filter(path=>path.includes("/")).map(path=>path.split("/")[0]));
      const folders=[...names].sort().map(name=>({name,path:`${prefix}${name}`}));
      return {folders,total_count:folders.length,page:1,page_size:30};
    }
    const match=path.match(/^logs\/([^/]+)\/(tail|history|forward|window|index)$/);
    if(match) {
      const log=logs.get(decodeURIComponent(match[1]));
      if(!log) return null;
      const count=Math.min(2000,Math.max(1,number(params,"lines",200))),length=log.lines.length;
      if(match[2]==="tail") return windowOf(log,Math.max(0,length-count),length);
      if(match[2]==="history") {
        const before=number(params,"before",log.size),end=log.lines.findIndex(line=>line.start>=before);
        const index=end===-1 ? length : end;
        return windowOf(log,Math.max(0,index-count),index);
      }
      if(match[2]==="forward") {
        const after=number(params,"after",0),found=log.lines.findIndex(line=>line.start>=after),index=found===-1 ? length : found;
        return windowOf(log,index,index+count);
      }
      if(match[2]==="index") return {status:"ready",generation:log.generation,total_lines:length,progress:1};
      let index;
      if(params.has("line")) index=Math.min(length-1,Math.max(0,Math.floor(number(params,"line",1))-1));
      else {const offset=log.size*Math.min(100,Math.max(0,number(params,"percent",50)))/100; index=log.lines.findIndex(line=>line.end>=offset); if(index<0) index=length-1;}
      const begin=Math.max(0,Math.min(length-count,index-Math.floor(count/2)));
      return windowOf(log,begin,begin+count,{offset:log.lines[index]?.start ?? 0,line_number:index+1});
    }
    return null;
  }
  function apiPath(input) {
    const url=new URL(input instanceof Request ? input.url : String(input),base);
    return url.origin===apiBase.origin && url.pathname.startsWith(apiBase.pathname) ? {url,path:url.pathname.slice(apiBase.pathname.length)} : null;
  }
  window.fetch=async function(input,options={}) {
    const match=apiPath(input);
    if(!match) return nativeFetch(input,options);
    if(options.signal?.aborted) throw new DOMException("The operation was aborted.","AbortError");
    const payload=answer(match.path,match.url.searchParams);
    return new Response(JSON.stringify(payload ?? {detail:"This route is not available in the browser demo."}),{status:payload ? 200 : 404,headers:{"Content-Type":"application/json"}});
  };
  class DemoEventSource extends EventTarget {
    static CONNECTING=0; static OPEN=1; static CLOSED=2;
    constructor(input,options) {
      const match=apiPath(input);
      if(!match || !/^logs\/[^/]+\/stream$/.test(match.path)) return new NativeEventSource(input,options);
      super();
      this.url=String(input);this.withCredentials=false;this.readyState=0;
      this.fileId=decodeURIComponent(match.path.split("/")[1]);this.cursor=number(match.url.searchParams,"cursor",0);
      this.openTimer=setTimeout(()=>{
        if(this.readyState===2) return;
        this.readyState=1;streams.add(this);const event=new Event("open");this.onopen?.(event);this.dispatchEvent(event);this.publish();
      },80);
    }
    emit(type,data) {this.dispatchEvent(new MessageEvent(type,{data:JSON.stringify(data)}));}
    publish() {
      if(this.readyState!==1) return;
      const log=logs.get(this.fileId);if(!log) return;
      this.emit("status",{file_id:this.fileId,connected:true,exists:true,size_bytes:log.size,mtime:log.mtime,generation:log.generation});
      const lines=log.lines.filter(line=>line.start>=this.cursor);
      if(lines.length) {this.cursor=log.size;this.emit("append",{file_id:this.fileId,generation:log.generation,lines,cursor:log.size});}
    }
    close() {this.readyState=2;clearTimeout(this.openTimer);streams.delete(this);}
  }
  window.EventSource=DemoEventSource;
  function tick() {
    if(document.hidden) return;
    for(const log of logs.values()) {
      if(log.lines.length>=3600) {
        log.lines=[];log.size=0;log.revision+=1;log.generation=`demo-${log.spec.id}-${log.revision}`;
        for(let n=0;n<600;n++) append(log);
        for(const stream of [...streams]) if(stream.fileId===log.spec.id) {stream.cursor=log.size;stream.emit("reset",{file_id:log.spec.id,generation:log.generation});}
      }
      append(log);
    }
    for(const stream of [...streams]) stream.publish();
  }
  function startTicker() {if(ticker===null) ticker=setInterval(tick,1500);}
  window.addEventListener("pagehide",()=>{clearInterval(ticker);ticker=null;for(const stream of [...streams]) stream.close();});
  window.addEventListener("pageshow",startTicker);
  startTicker();
  document.addEventListener("click",event=>{
    const link=event.target.closest?.("#download-log");
    if(!link) return;
    const match=apiPath(link.href),id=match ? decodeURIComponent(match.path.split("/")[1]) : "",log=logs.get(id);
    if(!log) return;
    event.preventDefault();
    const blob=new Blob([log.lines.map(line=>line.text).join("")],{type:"text/plain;charset=utf-8"}),url=URL.createObjectURL(blob),download=document.createElement("a");
    download.href=url;download.download=`demo-${log.spec.path.split("/").at(-1)}`;download.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  },true);
})();
