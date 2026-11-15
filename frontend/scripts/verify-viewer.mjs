import { createServer } from 'vite';
import { fileURLToPath } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';
const server=await createServer({root:fileURLToPath(new URL('../',import.meta.url)),server:{middlewareMode:true},appType:'custom'});
try {
 const {defaultView,readView,viewToSearch}=await server.ssrLoadModule('/src/lib/viewState.ts');
 const {parseCommands}=await server.ssrLoadModule('/src/lib/commands.ts');
 const {buildSequence}=await server.ssrLoadModule('/src/lib/sequence.ts');
 const {ViewControls}=await server.ssrLoadModule('/src/components/ViewControls.tsx');
 const {DetectionsPanel}=await server.ssrLoadModule('/src/components/DetectionsPanel.tsx');
 let v={...defaultView({ra:133.76002,dec:-7.24221,label:'test'}),mode:'visits',wise:true,dfloor:0,dgamma:1.7,diffLocal:true,sel:{ra:10,dec:20}};
 for(const section of ['observe','evidence','spectrum','prediction','sky']) { const state={...v,section,fp:true};assert.equal(readView(viewToSearch(state)).section,section); }
 for(const k of ['mode','wise','dfloor','dgamma','diffLocal','sel']) assert.deepEqual(readView(viewToSearch(v))[k],v[k]);
 assert.equal(parseCommands('visits allwise on',defaultView()).patch.wise,true);
 assert.equal(parseCommands('visits allwise on',defaultView()).patch.mode,'visits');
 const reset=parseCommands('reset',v).patch;assert.equal(reset.wise,false);assert.equal(reset.dfloor,1.5);
 const field=await (await fetch('http://127.0.0.1:8011/api/frames?ra=133.76002&dec=-7.24221&fov=5')).json();
 const counts={};
 for(const mode of ['stacks','visits','bins','diff','frames','rgb']) {
  v={...v,mode};const seq=buildSequence(field,v);counts[mode]=seq.items.length;assert(seq.items.length>0,mode);
  const html=renderToStaticMarkup(createElement(ViewControls,{view:v,field,seq,seasonLabel:String,update:()=>{},overlay:{loading:false,failed:null,data:null}}));
  assert(html.includes('Visits')&&html.includes('Show AllWISE sources'));
  if(mode==='diff')assert(html.includes('Colour contrast')&&html.includes('Colour from')&&html.includes('Scale colour by the local noise'));
 }
 const candidate=field.analysis.candidates.find(c=>c.evidence!=='weak');assert(candidate);
 const overlay={catalog:{ok:false},sources:[],candidates:{},notes:[],wise:{catalog:{ok:true},sources:[],candidates:{[candidate.id]:{designation:'known-source'}}}};
 const html=renderToStaticMarkup(createElement(DetectionsPanel,{analysis:field.analysis,seasonLabel:String,view:v,update:()=>{},overlay}));assert(html.includes('known-source'),'AllWISE match must survive Gaia outage');
 console.log('PASS: shared links, commands/reset, six mode sequences, rendered controls, AllWISE badge with Gaia down');console.log(counts);
} finally { await server.close(); }
