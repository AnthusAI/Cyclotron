import {useEffect,useState} from 'react'
import {graphql} from './graphql'
import type {Definition} from './ScorecardEditor'

export function ClassifierImpact({classifierId}:{classifierId:string}){
  const [cards,setCards]=useState<Definition[]|null>(null),[error,setError]=useState('')
  useEffect(()=>{
    let cancelled=false
    graphql<{scorecardDefinitions:Definition[]}>('{scorecardDefinitions}').then(result=>{
      if(!cancelled)setCards(result.scorecardDefinitions.filter(card=>card.classifiers.some(ref=>ref.id===classifierId)))
    }).catch(e=>{if(!cancelled)setError(e.message)})
    return()=>{cancelled=true}
  },[classifierId])
  return <section aria-label="Classifier change impact" className="space-y-2 rounded-md border bg-muted/30 p-3 text-sm">
    <h3 className="font-semibold">Affected scorecards</h3>
    {error?<p role="alert">Could not load affected scorecards: {error}</p>:cards===null?<p role="status">Checking scorecard membership…</p>:cards.length?<>
      <p>Saving a changed classifier creates a new definition for each of these scorecards. Their shared decision requests can affect all member classifiers.</p>
      <ul className="space-y-1">{cards.map(card=><li key={card.id}>{card.name} · revision {card.revision}</li>)}</ul>
    </>:<p>This classifier is not in an active scorecard definition.</p>}
    <p className="text-xs text-muted-foreground">Existing runs keep their pinned definitions and learned checkpoints. This edit does not restart or re-optimize them.</p>
  </section>
}
