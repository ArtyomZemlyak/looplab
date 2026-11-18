import test from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { fetchStub, mountLive, until, click } from './_mount.js'
const unknown={known_sum:null,known_count:0,unknown_count:1,complete:false}
const known=n=>({known_sum:n,known_count:1,unknown_count:0,complete:true})
const summary={state:'recorded',llm:[
  {observation_level:'provider_observed',tokens:{input_tokens:known(11),output_tokens:known(7),total_tokens:known(18)},cost_by_currency:[{currency:'USD',known_sum:.01}],cost_unknown_count:0},
  {observation_level:'agent_declared',tokens:{input_tokens:unknown,output_tokens:unknown,total_tokens:unknown},cost_by_currency:[],cost_unknown_count:1}],
  agent_reported_wall_seconds:known(4.5),native:{evaluation_wall_seconds:{value:2},gpu_hours:{value:null},llm:{totals:{cost:.03}}}}
const row=(id,revision,attempt=0)=>({receipt_id:id,node_id:0,attempt,revision_id:revision,recorded_at:'2026-11-11',
  revision:{provider:'fixture-provider',model:'fixture-model',prompt_sha256:revision,skills_sha256:null},observation_level:'provider_observed',
  outcome:attempt===0?{state:'historical_attempt',metric:null,status:null}:{state:'current_attempt',metric:.6,status:'evaluated'}})

test('separates LLM, wall and GPU unknowns and follows exact receipt pages without writes',async()=>{
  const h=await mountLive()
  try{
    const {default:View}=await h.load('/src/ExternalTelemetry.jsx');const requests=[]
    globalThis.fetch=fetchStub({'GET /api/runs/r/external-telemetry':({url})=>{
      requests.push(url);const old=url.searchParams.get('offset')==='20'
      return {...summary,items:[row(old?'old':'new',(old?'a':'b').repeat(64),old?0:1)],next_offset:20,has_more:!old,prefix_digest:'c'.repeat(64)}
    }})
    const view=await h.mount(View,{runId:'r',generation:'d'.repeat(64),summary})
    try{
      assert.equal(globalThis.fetch.calls.length,0)
      assert.match(view.container.textContent,/Input tokens: unknown/)
      assert.match(view.container.textContent,/GPU hours: unknown/)
      assert.match(view.container.textContent,/0.01 USD/)
      assert.match(view.container.textContent,/Native LLM cost ledger: 0.03/)
      assert.match(view.container.textContent,/no combined total/)
      await click([...view.container.querySelectorAll('button')].find(b=>b.textContent==='Read harness revisions'))
      await until(()=>view.container.textContent.includes('b'.repeat(64)),'current revision')
      assert.match(view.container.textContent,/fixture-provider \/ fixture-model/)
      assert.match(view.container.textContent,/evaluated · 0.6/)
      await click([...view.container.querySelectorAll('button')].find(b=>b.textContent==='Next receipt page'))
      await until(()=>view.container.textContent.includes('a'.repeat(64)),'original revision')
      assert.match(view.container.textContent,/Historical attempt; current outcome withheld/)
      assert.equal(requests.at(-1).searchParams.get('expected_prefix'),'c'.repeat(64))
      assert.equal(requests.at(-1).searchParams.get('offset'),'20')
      assert.ok(globalThis.fetch.calls.every(call=>call.method==='GET'))
    }finally{await view.unmount()}
  }finally{await h.close()}
})

test('changed or unavailable receipt page requires explicit first-page refresh',async()=>{
  const h=await mountLive()
  try{
    const {default:View}=await h.load('/src/ExternalTelemetry.jsx')
    globalThis.fetch=fetchStub({'GET /api/runs/r/external-telemetry':()=>{throw new Error('changed prefix')}})
    const view=await h.mount(View,{runId:'r',generation:'d'.repeat(64),summary})
    try{
      await click([...view.container.querySelectorAll('button')].find(b=>b.textContent==='Read harness revisions'))
      await until(()=>view.container.textContent.includes('Harness receipts changed or are unavailable'),'unavailable')
      assert.equal([...view.container.querySelectorAll('button')].some(b=>b.textContent==='Next receipt page'),false)
      assert.match(view.container.textContent,/first page again/)
      assert.ok(globalThis.fetch.calls.every(call=>call.method==='GET'))
    }finally{await view.unmount()}
  }finally{await h.close()}
})
