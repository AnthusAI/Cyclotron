import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { expect, test, vi } from "vitest";
import { CycleTimeline } from "./cycle-timeline";

test("a timeline projects model, human, optimization, and evaluation events into a cycle", () => {
  const onCycleSelect = vi.fn();
  render(<CycleTimeline activeCycle={1} onCycleSelect={onCycleSelect} events={[
    { sequence: 1, payload: { kind: "prediction", cycle_number: 1 } },
    { sequence: 2, payload: { kind: "human-feedback", cycle_number: 1 } },
    { sequence: 3, payload: { kind: "trigger-evaluated", due: true, cycle_number: 1 } },
    { sequence: 4, payload: { kind: "cycle-metrics", cycle_number: 1 } },
  ]} />);
  expect(screen.getByRole("button", { name: "Model decision, cycle 1: recorded" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Human label, cycle 1: recorded" })).toBeVisible();
  expect(screen.getByRole("button", { name: "Optimization, cycle 1: recorded" })).toBeVisible();
  fireEvent.click(screen.getByText("1"));
  expect(onCycleSelect).toHaveBeenCalledWith(expect.objectContaining({ number: 1, sequence: 4 }));
});
