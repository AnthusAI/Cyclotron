import { render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { expect, test } from "vitest";
import { LabMetrics } from "./lab-metrics";

test("a lab monitor presents recall, precision, and accuracy in that order", () => {
  render(<LabMetrics metrics={[
    { name: "Recall", before: 0.4, after: 0.6 },
    { name: "Precision", before: 0.5, after: 0.7 },
    { name: "Accuracy", before: 0.6, after: 0.8 },
  ]} />);
  expect(screen.getAllByText(/Recall|Precision|Accuracy/).map((node) => node.textContent)).toEqual(["Recall", "Precision", "Accuracy"]);
  expect(screen.getAllByText("+20.0 pp")).toHaveLength(3);
});
