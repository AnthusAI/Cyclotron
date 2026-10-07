export type CycleTimelineEvent = {
  sequence: number;
  payload: Record<string, unknown>;
};

type Cycle = {
  number: number;
  sequence: number;
  decision: boolean;
  feedback: boolean;
  optimization: boolean;
  evaluation: boolean;
};

const kind = (event: CycleTimelineEvent) => String(event.payload.kind ?? "");

/**
 * A compact, event-backed view of the same cycles shown in the lab timeline.
 * It accepts recorded events rather than a backend client, so live and
 * illustrative surfaces use the identical projection and visual semantics.
 */
export function CycleTimeline({
  events,
  activeCycle,
  onCycleSelect,
}: {
  events: CycleTimelineEvent[];
  activeCycle?: number;
  onCycleSelect?: (cycle: { number: number; sequence: number }) => void;
}) {
  const cycles = new Map<number, Cycle>();
  for (const event of events) {
    const number = Number(event.payload.cycle_number);
    if (!Number.isInteger(number) || number < 1) continue;
    const current = cycles.get(number) ?? { number, sequence: event.sequence, decision: false, feedback: false, optimization: false, evaluation: false };
    current.sequence = Math.max(current.sequence, event.sequence);
    const eventKind = kind(event);
    current.decision ||= eventKind === "prediction" || eventKind === "decision-response";
    current.feedback ||= eventKind === "human-feedback";
    current.optimization ||= eventKind.startsWith("optimizer-") || eventKind.startsWith("optimization-") || eventKind === "trigger-evaluated" && event.payload.due === true;
    current.evaluation ||= eventKind === "cycle-metrics" || eventKind === "candidate-evaluated" || eventKind === "fit-completed";
    cycles.set(number, current);
  }
  const rows: Array<[string, keyof Omit<Cycle, "number" | "sequence">]> = [
    ["Model decision", "decision"],
    ["Human label", "feedback"],
    ["Optimization", "optimization"],
    ["Evaluation", "evaluation"],
  ];
  const visible = [...cycles.values()].sort((left, right) => left.number - right.number);
  if (!visible.length) return <p className="text-sm text-muted-foreground">No recorded cycles yet.</p>;
  return <div className="overflow-x-auto" aria-label="Cyclotron run timeline">
    <div className="min-w-[36rem]" style={{ display: "grid", gridTemplateColumns: `8rem repeat(${visible.length}, minmax(2rem, 1fr))` }}>
      <div className="bg-secondary p-2 text-xs font-medium text-muted-foreground">Cycle</div>
      {visible.map((cycle) => <button key={cycle.number} type="button" onClick={() => onCycleSelect?.(cycle)} aria-current={activeCycle === cycle.number ? "step" : undefined} className={`min-h-9 p-1 text-xs font-medium ${activeCycle === cycle.number ? "bg-primary text-primary-foreground" : "bg-secondary text-secondary-foreground hover:bg-muted"}`}>{cycle.number}</button>)}
      {rows.flatMap(([label, property]) => [
        <div key={`${property}-label`} className="bg-muted p-2 text-xs text-muted-foreground">{label}</div>,
        ...visible.map((cycle) => <button key={`${property}-${cycle.number}`} type="button" onClick={() => onCycleSelect?.(cycle)} aria-label={`${label}, cycle ${cycle.number}: ${cycle[property] ? "recorded" : "not recorded"}`} className={cycle[property] ? "min-h-9 bg-success" : "min-h-9 bg-muted"} />),
      ])}
    </div>
  </div>;
}
