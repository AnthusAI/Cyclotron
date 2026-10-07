import {readFileSync} from "node:fs";
import {expect, test} from "vitest";

test("the shared brand stylesheet keeps light and dark surfaces polarity consistent", () => {
  const css = readFileSync("src/styles/shared.css", "utf8");
  expect(css).toContain("--primary-foreground: oklch(0.18");
  expect(css).toContain("--primary-foreground: oklch(0.94");
  expect(css).toContain("background-image: none !important");
});
