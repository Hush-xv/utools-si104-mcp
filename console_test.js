// Focused DOM-level regression checks; run with node console_test.js.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag = 'div') { this.tag=tag; this.children=[]; this.textContent=''; this.value=''; this.dataset={}; this.classes=new Set(); this.classList={add:x=>this.classes.add(x),toggle:(x,on)=> {
   if (on === undefined) on = !this.classes.has(x) && !this.className?.split(' ').includes(x);
   this.className = (this.className || '').split(' ').filter(name => name !== x).join(' ');
   if (on) this.classes.add(x); else this.classes.delete(x);
   return on;
  }}; }
  append(...items){this.children.push(...items)} replaceChildren(...items){this.children=items} add(item){this.children.push(item)}
  prepend(...items){this.children.unshift(...items)}
  setAttribute(key,value){this[key]=value}
  querySelector(){return null}
  contains(element){return element===this || this.children.some(child=>child.contains(element))}
  get firstChild(){return this.children[0]}
  get childElementCount(){return this.children.length}
  getBoundingClientRect(){return {height:24}}
}
const ids=['operationStatus','scanAddresses','history','registerRows','registerContext','device','address','addrBits','pollToggle','pollInterval','mode','busHz','reg','regBits','deviceIdentity','deviceInfo'];
const nodes=Object.fromEntries(ids.map(id=>[id,new Element()]));nodes.address.value='0x42';nodes.addrBits.value='7';nodes.mode.value='0';nodes.busHz.value='100000';nodes.pollInterval.value='500';
nodes.reg.value='0x0010';
nodes.regBits.value='16';
nodes.history.parentElement={parentElement:{scrollTop:0,getBoundingClientRect:()=>({bottom:300})}};
const storage=new Map(),source=fs.readFileSync('si104_console/console.html','utf8');
const start=source.indexOf('function hexAddress('),end=source.indexOf('async function controlVcc(',start);assert.ok(start>=0&&end>start);
const ctx={document:{createElement:tag=>new Element(tag)},$:id=>nodes[id],window:{alert(){}},localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,val)=>storage.set(key,val)},Option:class{constructor(text,value){this.text=text;this.value=value}},debug(){},setBusy(){},ensure(){},run(){},saveDraft(){},loadDraft(){},toggleTab(){},serial:()=>'ABC',target:()=>0x42,number:(id,min,max)=>{const n=Number(nodes[id].value);assert.ok(n>=min&&n<=max);return n},common:()=>({serial:'ABC',channel:ctx.channel,mode:0,bus_hz:100000}),call:async()=>({ok:false,data:{error:'timeout',firmware_status:8}}),devices:[],channel:0,busy:false,polling:false,pollTimer:null,rowSeq:0,loadedRegisterKey:undefined,registerRows:[],history:[],drafts:[{},{},{},{}]};
vm.createContext(ctx);vm.runInContext(source.slice(start,end),ctx);ctx.loadRegisterRows();
const entry=(name,args,data,ok=true)=>({name,args,data,ok,label:name,time:'12:00',channel:'I²C 0',ms:1});
ctx.show(entry('si104_i2c_scan',{channel:0,serial:'ABC'},{found:[]}));assert.match(nodes.operationStatus.children[0].textContent,/未发现 ACK/);
ctx.show(entry('si104_i2c_scan',{channel:0,serial:'ABC'},{error:'scan aborted at 0x08: NO_POWER',firmware_status:11,found_before_abort:[]},false));assert.match(nodes.operationStatus.children[0].textContent,/扫描中止于 0x08/i);assert.match(nodes.operationStatus.children[0].textContent,/NO_POWER/);assert.match(nodes.operationStatus.className,/error/);assert.ok(nodes.operationStatus.children.some(x=>x.textContent==='使能共享 VCC'));assert.doesNotMatch(nodes.operationStatus.children[0].textContent,/扫描完成/);
ctx.show(entry('si104_spi_transfer',{tx_hex:'A55A'},{rx_hex:'a55a'}));assert.match(nodes.operationStatus.children[0].textContent,/回环 OK/);assert.match(nodes.operationStatus.children[0].textContent,/不能单独证明硬件完好/);
ctx.addRegister();assert.equal(ctx.registerRows[0].reg,'0x0010');assert.equal(ctx.registerRows[0].width,16);assert.equal(ctx.registerRows.length,1);ctx.renderRegisterRows();const rowElement=nodes.registerRows.children[0],valueInput=rowElement.children[1].children[0];valueInput.value='77';valueInput.oninput();assert.equal(ctx.rowArgs(ctx.registerRows[0],'write').args.data_hex,'77','row write must use the live edited value');const regInput=rowElement.children[0].children[0];regInput.value='0x11';regInput.oninput();assert.equal(ctx.rowArgs(ctx.registerRows[0],'write').reg,0x11,'row write must use the live edited register');assert.equal(ctx.rowArgs(ctx.registerRows[0],'write').args.address,0x42,'register rows must use the one shared target address');ctx.saveRegisterRows();nodes.address.value='0x43';ctx.loadRegisterRows();assert.equal(ctx.registerRows.length,0);nodes.address.value='0x42';ctx.loadRegisterRows();assert.equal(ctx.registerRows[0].value,'77');assert.equal(ctx.registerRows[0].status,'待写入');
assert.equal(ctx.firmwareLabel({version_hex:'01030300',info:{firmware_version:19792540}}),'1.3.0 (01030300)');
ctx.logEntry(entry('si104_i2c_read_regs',{address:66,reg:3,size:16},{error:'failed'},false));assert.equal(nodes.history.children[0].children[3].textContent,'0x42');assert.equal(nodes.history.children[0].children[4].textContent,'0x3');
const oldRow = nodes.history.children[0];
oldRow.onclick();
const oldDetail = nodes.history.children[1];
assert.equal(oldRow['aria-expanded'],'true');
ctx.logEntry(entry('si104_spi_transfer',{tx_hex:'41'.repeat(18)},{rx_hex:'42'.repeat(18)}));
assert.equal(nodes.history.children[2],oldRow);
assert.equal(nodes.history.children[3],oldDetail);
assert.equal(oldRow['aria-expanded'],'true');
const full = nodes.history.children[0];full.onclick();
const detail = nodes.history.children[1].children[0].children[0];
assert.match(detail.children[0].children[0].children[0].textContent,/接收 HEX/);
assert.match(detail.children[1].children[0].children[0].textContent,/发送 HEX/);
const rxRows = detail.children[0].children[0].children[2].children;
assert.equal(rxRows[1].children[0].textContent,'0x0010');
assert.equal(rxRows[1].children[2].textContent,'42');
assert.equal(rxRows[1].children.at(-1).textContent,'BB');
const registerRead = entry('si104_i2c_read_regs',{address:66,reg:1},{data_hex:'5c',size:1});
const registerWrite = {
  ...entry('si104_i2c_transfer',{address:0x142,addr_10bit:true,tx_hex:'12345678',rx_size:0},{rx_hex:'',rx_bytes:0}),
  registerAction:'write',reg:0x1234,regBits:16,
};
for (const [item,title,bytes] of [[registerRead,'寄存器读回 HEX · 1 字节',['5C']], [registerWrite,'寄存器写入 HEX · 2 字节',['56','78']]]) {
  const content = new Element();
  ctx.renderTransactionDetail(content,item);
  const table = content.children[0].children[0];
  assert.equal(table.children[0].textContent,title);
  assert.deepEqual(table.children[2].children[0].children.slice(1,1+bytes.length).map(cell=>cell.textContent),bytes);
  assert.equal(content.children[1].tag,'dl');
}
ctx.logEntry(registerWrite);
assert.equal(nodes.history.children[0].children[5].textContent,2);
assert.equal(nodes.history.children[0].children[6].textContent,'5678');
const vcc = entry('si104_read_vcc',{channel:0},{measured_mv:3129,raw_hex:'0100b80c390c0000390c'});
const vccDetail = new Element();
ctx.renderTransactionDetail(vccDetail,vcc);
assert.equal(vccDetail.children[0].children[0].children[0].textContent,'VCC 回复 HEX · 10 字节');
assert.equal(vccDetail.children[1].tag,'dl');
const mismatch = entry('si104_vcc_control',{enable:true},{output_enabled:false,raw_hex:'0000b80b000000000000'});
assert.equal(ctx.vccMismatch(mismatch),true);
assert.match(ctx.failureText(mismatch),/使能未生效.*已关断/);
const failure = entry('si104_i2c_scan', {channel:0}, {code:-10, firmware_status:11, error:'scan aborted at 0X0a: device rejected command'}, false);
ctx.show(failure, '轮询已暂停：');
assert.match(nodes.operationStatus.children[0].textContent,/轮询已暂停：扫描中止于 0x0A/);
assert.doesNotMatch(nodes.operationStatus.children[0].textContent,/device rejected/);
assert.equal(nodes.operationStatus.children[1].textContent,'使能共享 VCC');
console.log('console regression checks passed');
