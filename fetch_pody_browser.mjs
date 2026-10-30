#!/usr/bin/env node
/** Public Pody DOM collector. Node 22 + Playwright (installed runtime is recorded).
 * No curl/requests/HTML fetch, login, hidden API invocation, playback or audio download.
 * Usage: PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node fetch_pody_browser.mjs
 * --url URL --out FILE --limit 100 --max-pages 10 --timeout-ms 60000 --no-details
 * --schema prints the JSON Schema without launching a browser.
 * Exit 0 = complete list and requested detail checks; 2 = partial; 1 = fatal.
 */
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
const args = process.argv.slice(2);
function option(name, fallback) {
  const i = args.indexOf(name);
  if (i < 0) return fallback;
  if (!args[i + 1] || args[i + 1].startsWith('--')) throw Error(`Missing value: ${name}`);
  return args[i + 1];
}
const nullableString = {type:['string','null']};
const schema = {
  $schema:'https://json-schema.org/draft/2020-12/schema', type:'object',
  required:['schema_version','source_url','status','total','articles','pagination','errors'],
  properties:{
    schema_version:{const:1}, source_url:{type:'string',format:'uri'},
    observed_at:{type:'string',format:'date-time'}, finished_at:{type:'string',format:'date-time'},
    status:{enum:['ok','partial','failed']}, total:{type:'integer',minimum:0},
    article_available_count:{type:'integer',minimum:0}, runtime:{type:'object'}, robots:{type:'object'},
    errors:{type:'array',items:{type:'object',required:['stage','message'],properties:{stage:{type:'string'},message:{type:'string'}}}},
    pagination:{type:'object',required:['complete','reason','pages'],properties:{
      complete:{type:'boolean'},reason:{type:'string'},expected_count:{type:['integer','null']},
      pages:{type:'array',items:{type:'object'}},limits:{type:'object'}
    }},
    articles:{type:'array',items:{type:'object',required:['title','href','has_article','raw_li_text'],properties:{
      title:{type:'string',minLength:1},href:{type:'string',format:'uri'},href_raw:{type:'string'},
      has_article:{type:'boolean'},raw_li_text:{type:'string'},published_display:nullableString,
      duration:nullableString,detail:{type:['object','null'],properties:{
        status:{enum:['ok','failed']},http_status:{type:'integer'},final_url:{type:'string'},
        article_heading:nullableString,canonical:nullableString,article_jsonld:{type:'boolean'},
        audio_urls:{type:'array',items:{type:'string',format:'uri'}},rss_guid:nullableString,
        rss_guid_status:{enum:['found','not_exposed_in_public_dom']},
        identifier_evidence:{type:'array'},jsonld_errors:{type:'array'},error:{type:'string'}
      }}
    }}}
  }
};
if (args.includes('--schema')) { console.log(JSON.stringify(schema,null,2)); process.exit(0); }
const source = option('--url','https://pody.jp/player/lfT9M8syTrRnbeinQJFW?kind=article');
const output = option('--out',new URL('./pody_browser_observation.json',import.meta.url).pathname);
const limit = Number(option('--limit','100'));
const maxPages = Number(option('--max-pages','10'));
const timeout = Number(option('--timeout-ms','60000'));
for (const [k,v] of Object.entries({limit,maxPages,timeout})) if (!Number.isInteger(v)||v<1) throw Error(`Invalid ${k}`);
const url = new URL(source);
if (url.origin!=='https://pody.jp'||!/^\/player\/[^/]+\/?$/.test(url.pathname)||url.searchParams.get('kind')!=='article') throw Error('Only public https://pody.jp/player/PROGRAM?kind=article is supported');
const prefix = url.pathname.replace(/\/$/,'')+'/';
const result = {schema_version:1,source_url:source,observed_at:new Date().toISOString(),status:'failed',
  total:0,article_available_count:0,runtime:{node:process.version},robots:{},articles:[],
  pagination:{complete:false,reason:'not_started',expected_count:null,pages:[],limits:{limit,max_pages:maxPages,timeout_ms:timeout}},errors:[]};
let browser;
let rules=[];
function permitted(target) {
  const u=new URL(target);
  if(u.origin!==url.origin)return false;
  const path=u.pathname+u.search;
  const matches=rules.filter(r=>r.regex.test(path)).sort((a,b)=>b.length-a.length||Number(b.allow)-Number(a.allow));
  return !matches.length||matches[0].allow;
}
function parseRobots(text) {
  const groups=[];let group=null;let hasRules=false;
  for(const raw of text.split(/\r?\n/)){
    const line=raw.replace(/#.*/,'').trim(); const m=line.match(/^([^:]+):\s*(.*)$/);if(!m)continue;
    const key=m[1].toLowerCase(),value=m[2].trim();
    if(key==='user-agent') {if(!group||hasRules){group={agents:[],rules:[]};groups.push(group);hasRules=false;}group.agents.push(value.toLowerCase());}
    else if(group&&['allow','disallow'].includes(key)){hasRules=true;if(value)group.rules.push({allow:key==='allow',path:value});}
  }
  // Browser default user agent identifies Chrome/Chromium, not an invented crawler.
  const specific=groups.filter(g=>g.agents.some(a=>a!=='*'&&/chrome|chromium|mozilla/.test(a)));
  const selected=specific.length?specific:groups.filter(g=>g.agents.includes('*'));
  return selected.flatMap(g=>g.rules).map(r=>({...r,length:r.path.replace(/[$*]/g,'').length,
    regex:new RegExp('^'+r.path.replace(/[.+?^{}()|[\]\\]/g,'\\$&').replace(/\*/g,'.*'))}));
}
async function navigate(page,target){
  if(!permitted(target))throw Error(`robots disallows or non-public origin: ${target}`);
  const response=await page.goto(target,{waitUntil:'domcontentloaded',timeout});
  if(!response||response.status()>=400)throw Error(`HTTP ${response?.status()} ${target}`);
  if(!permitted(page.url())||/\/login(?:[/?]|$)/.test(page.url()))throw Error(`Access redirect: ${page.url()}`);
  return response;
}
async function snapshot(page){
  return page.evaluate(prefix=>{
    const linksFor=li=>[...li.querySelectorAll('a[href]')].filter(a=>a.origin===location.origin&&a.pathname.startsWith(prefix)&&a.pathname.slice(prefix.length).match(/^[^/]+$/));
    const items=[...document.querySelectorAll('li')].flatMap(li=>{
      const links=linksFor(li);if(!links.length)return [];
      const a=links.find(a=>a.querySelector('p'))||links.find(a=>a.textContent.trim());if(!a)return [];
      const text=li.innerText;
      return [{title:(a.querySelector('p')?.textContent||a.textContent).trim(),href:a.href,href_raw:a.getAttribute('href'),
        has_article:[...li.querySelectorAll('span')].some(e=>e.textContent.trim()==='記事あり'),raw_li_text:text,
        published_display:text.match(/公開日\s*([^\n]+)/)?.[1]??null,duration:text.match(/\b\d{2}:\d{2}:\d{2}\b/)?.[0]??null,detail:null}];
    });
    const expected=document.body.innerText.match(/記事で読めるエピソード\s*[（(]\s*([\d,]+)件\s*[）)]/);
    const controls=[...document.querySelectorAll('a,button,[role="button"]')].filter(e=>e.getClientRects().length).map(e=>({
      tag:e.tagName,text:e.innerText.trim(),label:e.getAttribute('aria-label'),title:e.getAttribute('title'),rel:e.getAttribute('rel'),
      href:e.href??null,disabled:!!e.disabled||e.getAttribute('aria-disabled')==='true'
    }));
    const forward=controls.filter(e=>e.rel==='next'||/^(次(?:へ|のページ)?|もっと見る|さらに表示|続きを読み込む|次の\d+件|next(?: page)?|load more|もっと読み込む|さらに読み込む)$/i.test(e.text||e.label||e.title||''));
    const paginationRegions=[...document.querySelectorAll('nav,[aria-label],[data-testid]')].filter(e=>/pagination|ページネーション|ページ送り/i.test([e.getAttribute('aria-label'),e.getAttribute('data-testid'),e.className].join(' '))).map(e=>e.innerText);
    return {items,expected:expected?Number(expected[1].replaceAll(',','')):null,controls,forward,pagination_regions:paginationRegions,
      footer_visible:!!document.querySelector('footer'),scroll_height:document.documentElement.scrollHeight};
  },prefix);
}
try {
  const mod=process.env.PLAYWRIGHT_MODULE;
  let pw;
  try { pw=await import(mod?(mod.startsWith('/')?pathToFileURL(mod).href:mod):'playwright'); }
  catch(e){throw Error(`Playwright unavailable. Install externally; set PLAYWRIGHT_MODULE=/absolute/node_modules/playwright/index.mjs. ${e.message}`);}
  try {const require=createRequire(import.meta.url);result.runtime.playwright=require(mod?mod.replace(/index\.mjs$/,'package.json'):'playwright/package.json').version;}catch{}
  browser=await pw.chromium.launch({headless:true});result.runtime.browser=browser.version();
  const context=await browser.newContext({viewport:{width:1280,height:900},locale:'ja-JP'});
  // Do not download media, fonts or images; page scripts still execute normally.
  await context.route('**/*',route=>['media','image','font'].includes(route.request().resourceType())?route.abort():route.continue());
  const page=await context.newPage();page.setDefaultTimeout(timeout);
  const rr=await page.goto(url.origin+'/robots.txt',{waitUntil:'domcontentloaded',timeout});
  if(!rr||(!rr.ok()&&rr.status()!==404))throw Error(`robots unavailable: HTTP ${rr?.status()}`);
  const robotsText=rr.status()===404?'':await page.locator('body').innerText();
  rules=parseRobots(robotsText);result.robots={url:url.origin+'/robots.txt',http_status:rr.status(),text:robotsText,source_allowed:permitted(source)};
  await navigate(page,source);
  await page.waitForFunction(prefix=>[...document.querySelectorAll('li a[href]')].some(a=>a.pathname.startsWith(prefix)&&a.querySelector('p')),prefix,{timeout});
  const seen=new Map();const visited=new Set();
  for(let pageNumber=1;pageNumber<=maxPages;pageNumber++){
    let snap;let previous='';let stable=0;const samples=[];
    for(let round=0;round<12;round++){
      snap=await snapshot(page);
      for(const item of snap.items)seen.set(item.href,item);
      const signature=JSON.stringify(snap.items.map(i=>i.href));stable=signature===previous?stable+1:0;previous=signature;
      samples.push({round,count:snap.items.length,unique_count:seen.size,height:snap.scroll_height});
      await page.evaluate(()=>{window.scrollTo(0,document.documentElement.scrollHeight);for(const e of document.querySelectorAll('main,ul,[role="list"]'))if(e.scrollHeight>e.clientHeight)e.scrollTop=e.scrollHeight;});
      if(stable>=3)break;
      await page.waitForTimeout(1200);
    }
    result.pagination.expected_count=snap.expected;
    const active=snap.forward.filter(c=>!c.disabled);
    result.pagination.pages.push({number:pageNumber,url:page.url(),dom_count:snap.items.length,unique_count:seen.size,
      scroll_samples:samples,controls:snap.controls,forward_controls:snap.forward,pagination_regions:snap.pagination_regions,footer_present:snap.footer_visible});
    result.articles=[...seen.values()].slice(0,limit);
    if(seen.size>limit||(seen.size===limit&&snap.expected!==seen.size)){result.pagination.reason='limit_reached';break;}
    if(snap.expected!==null&&seen.size===snap.expected&&active.length===0&&stable>=3){
      result.pagination.complete=true;result.pagination.reason='advertised_count_matched_and_stable_after_scroll_no_enabled_next';break;
    }
    if(!active.length){result.pagination.reason=snap.expected===null?'advertised_count_missing':'count_mismatch_without_next';break;}
    if(pageNumber===maxPages){result.pagination.reason='max_pages_reached';break;}
    const next=active[0];const key=JSON.stringify([page.url(),[...seen.keys()],next]);
    if(visited.has(key)){result.pagination.reason='pagination_loop';break;}visited.add(key);
    const old=JSON.stringify(snap.items.map(i=>i.href));
    if(next.href){await navigate(page,next.href);}else{
      // Only a recognized PUBLIC UI pagination control is clicked; never arbitrary instructions.
      const label=next.label||next.text||next.title;
      await page.getByRole('button',{name:label,exact:true}).click();
    }
    await page.waitForFunction(({prefix,old})=>JSON.stringify([...document.querySelectorAll('li')].flatMap(li=>{
      const a=[...li.querySelectorAll('a[href]')].find(a=>a.pathname.startsWith(prefix)&&a.querySelector('p'));return a?[a.href]:[];
    }))!==old,{prefix,old},{timeout});
  }
  if(!args.includes('--no-details'))for(const item of result.articles){
    try{
      await page.waitForTimeout(500);const response=await navigate(page,item.href);
      await page.waitForFunction(()=>document.querySelector('h1')&&document.querySelector('script[type="application/ld+json"]'),null,{timeout});
      const detail=await page.evaluate(()=>{
        const docs=[],errors=[];
        for(const e of document.querySelectorAll('script[type="application/ld+json"]'))try{docs.push(JSON.parse(e.textContent));}catch(e){errors.push(e.message);}
        const nodes=docs.flatMap(d=>Array.isArray(d)?d:(d['@graph']??[d]));
        const article=nodes.find(d=>[].concat(d['@type']??[]).includes('Article'));
        const audio=new Set();const identifiers=[];
        function walk(o,path){if(!o||typeof o!=='object')return;for(const [k,v] of Object.entries(o)){
          if(k==='contentUrl'&&typeof v==='string'&&/^https?:/.test(v))audio.add(v);
          if(/^(guid|rssGuid|episodeGuid|identifier)$/i.test(k))identifiers.push({path:path+'.'+k,value:v});
          if(typeof v==='object')walk(v,path+'.'+k);
        }}
        if(article?.isBasedOn)walk(article.isBasedOn,'Article.isBasedOn');
        for(const e of document.querySelectorAll('audio[src],audio source[src]'))audio.add(e.src);
        const guid=identifiers.find(i=>/\.(guid|rssGuid|episodeGuid)$/i.test(i.path));
        return {article_heading:document.querySelector('h1')?.textContent.trim()??null,
          canonical:document.querySelector('link[rel="canonical"]')?.href??null,article_jsonld:!!article,
          audio_urls:[...audio],audio_source:'public DOM JSON-LD Article.isBasedOn / audio element',
          rss_guid:typeof guid?.value==='string'?guid.value:null,rss_guid_status:guid?'found':'not_exposed_in_public_dom',
          identifier_evidence:identifiers,jsonld_errors:errors};
      });
      if(!detail.article_jsonld)throw Error('Article JSON-LD missing');
      item.detail={status:'ok',http_status:response.status(),final_url:page.url(),...detail};
    }catch(e){item.detail={status:'failed',error:e.message};result.errors.push({stage:'detail',href:item.href,message:e.message});
      if(/HTTP (401|403|429)|Access redirect|robots/.test(e.message))break;
    }
  }
  result.status=result.pagination.complete&&!result.errors.length?'ok':'partial';
}catch(e){result.errors.push({stage:'fatal',message:e.message});result.status='failed';}
finally{
  if(browser)await browser.close();
  result.total=result.articles.length;result.article_available_count=result.articles.filter(a=>a.has_article).length;
  result.finished_at=new Date().toISOString();
  // Only this designated result file is replaced on subsequent invocations.
  await fs.writeFile(output,JSON.stringify(result,null,2)+'\n');
  console.log(JSON.stringify({status:result.status,total:result.total,complete:result.pagination.complete,
    detail_ok:result.articles.filter(a=>a.detail?.status==='ok').length,errors:result.errors,output}));
  process.exitCode=result.status==='ok'?0:result.status==='partial'?2:1;
}
