import { Card, CardContent, CardHeader } from "./ui/card";

export type LabMetric = {
  name: "Recall" | "Precision" | "Accuracy";
  before?: number | null;
  after?: number | null;
};

const percent = (value: number | null | undefined) =>
  value == null ? "Undefined" : `${(value * 100).toFixed(1)}%`;

/** A reusable outcome monitor for a Cyclotron lab or recorded run. */
export function LabMetrics({ metrics }: { metrics: LabMetric[] }) {
  return <div className="grid grid-cols-1 gap-3 sm:grid-cols-3" aria-label="Lab outcome metrics">
    {metrics.map((metric) => {
      const delta = metric.before != null && metric.after != null
        ? (metric.after - metric.before) * 100 : null;
      return <Card key={metric.name} className="gap-2 py-4">
        <CardHeader className="px-4 pb-0"><p className="text-sm font-medium text-foreground">{metric.name}</p></CardHeader>
        <CardContent className="px-4">
          <div className="flex items-center justify-between gap-2 font-mono text-xl font-medium">
            <div><p className="mb-1 font-sans text-xs font-normal text-muted-foreground">Before</p>{percent(metric.before)}</div>
            <span className="text-muted-foreground">→</span>
            <div><p className="mb-1 font-sans text-xs font-normal text-muted-foreground">After</p>{percent(metric.after)}</div>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">{delta == null ? "Change unavailable" : `${delta >= 0 ? "+" : ""}${delta.toFixed(1)} pp`}</p>
        </CardContent>
      </Card>;
    })}
  </div>;
}
