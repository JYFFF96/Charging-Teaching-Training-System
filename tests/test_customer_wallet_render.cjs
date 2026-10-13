const fs=require('fs'),vm=require('vm'),path=require('path');
class Element{constructor(){this.innerHTML='';this.textContent='';this.value='';this.style={};this.classList={contains:()=>false};}querySelector(){return new Element()}querySelectorAll(){return []}reset(){}}
const elements=new Map(),element=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id)};
const context={console,URLSearchParams,AbortController,Date,Math,JSON,Number,String,Object,Array,location:{search:'?pile_no=32010200000001&gun_no=1'},document:{hidden:false,getElementById:element},setTimeout:()=>0,clearTimeout(){},setInterval:()=>0,fetch:async url=>({ok:!url.endsWith('/me'),status:url.endsWith('/me')?401:200,json:async()=>url.endsWith('/me')?{error:'请先登录'}:{insecure_test:true,test_balance_yuan:100}})};
vm.createContext(context);const html=fs.readFileSync(path.join(__dirname,'../customer/charge.html'),'utf8');vm.runInContext(html.split('<script>')[1].split('</script>')[0],context);
(async()=>{await new Promise(setImmediate);await new Promise(setImmediate);
vm.runInContext("user='alice';wallet={balance_fen:100000,available_fen:90000,reserved_fen:10000,ledger:[]};device={availability:'idle',latest:{gun_plugged:1}};page='device';render()",context);
let text=element('view').innerHTML;if(!text.includes('1000.00')||!text.includes('先拔枪再重新插枪'))throw Error('wallet or replug instructions');
vm.runInContext("order={status:'failed',pile_no:'32010200000001',gun_no:1,note:'未检测到有效插枪，请先拔枪再重新插好枪（原因码5）',device_state:'idle',latest:{},settlement_status:'not_charged'};page='charging';render()",context);
text=element('view').innerHTML;if(!text.includes('原因码5')||!text.includes('未扣费'))throw Error('failure notice');
vm.runInContext("order.status='finished';order.settlement_status='settled';order.settled_fen=1235;render()",context);
if(!element('view').innerHTML.includes('已模拟扣费：12.35'))throw Error('settled amount');
vm.runInContext("wallet.ledger=[{kind:'admin_credit',delta_fen:100,after_fen:100100,created:1,note:'<img src=x onerror=alert(1)>'}];page='wallet';render()",context);
text=element('view').innerHTML;if(!text.includes('&lt;img')||text.includes('<img src=x'))throw Error('unsafe ledger rendering');console.log('PASS customer balance, replug failure notice, settlement and ledger rendering');
})().catch(e=>{console.error(e);process.exit(1)});
