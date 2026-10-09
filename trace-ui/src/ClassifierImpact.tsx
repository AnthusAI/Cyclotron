import {useEffect,useState} from 'react'
import {graphql} from './graphql'
import type {Definition} from './CyclotronEditor'

export function ClassifierImpact({classifierId}:{classifierId:string}){
  const [cards,setCards]=useState<Definition[]|null>(null),[error,setError]=useState('')
  useEffect(()=>{
    let cancelled=false
    graphql<{cyclotronDefinitions:Definition[]}>('{cyclotronDefinitions}').then(result=>{
      if(!cancelled)setCards(result.cyclotronDefinitions.filter(card=>card.classifiers.some(ref=>ref.id===classifierId)))
    }).catch(e=>{if(!cancelled)setError(e.message)})
    return()=>{cancelled=true}
  },[classifierId])
  return <section aria-label="Classifier change impact" className="space-y-2 rounded-md border bg-muted/30 p-3 text-sm">
    <h3 className="font-semibold">Affected cyclotrons</h3>
    {error?<p role="alert">Could not load affected cyclotrons: {error}</p>:cards===null?<p role="status">Checking cyclotron membership…</p>:cards.length?<>
      <p>Saving a changed classifier creates a new definition for each of these cyclotrons. Their shared decision requests can affect all member classifiers.</p>
      <ul className="space-y-1">{cards.map(card=><li key={card.id}>{card.name} · revision {card.revision}</li>)}</ul>
    </>:<p>This classifier is not in an active cyclotron definition.</p>}
    <p className="text-xs text-muted-foreground">Existing runs keep their pinned definitions and learned checkpoints. This edit does not restart or re-optimize them.</p>
  </section>
}
