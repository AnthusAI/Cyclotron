import {readFileSync} from "node:fs";
import {expect, test} from "vitest";

test("the shared brand stylesheet keeps light and dark surfaces polarity consistent", () => {
  const css = readFileSync("src/styles/shared.css", "utf8");
  expect(css).toContain("--primary-foreground: oklch(0.18");
  expect(css).toContain("--primary-foreground: oklch(0.94");
  expect(css).toContain("background-image: none !important");
});

test('the viewport shell reserves device safe areas without adding document scroll',()=>{
  const css=readFileSync('src/index.css','utf8')
  expect(css).toContain('padding:env(safe-area-inset-top,0px) env(safe-area-inset-right,0px) env(safe-area-inset-bottom,0px) env(safe-area-inset-left,0px)')
  expect(css).toContain('html,body,#root {height:100%;overflow:hidden}')
  expect(css).toContain('box-sizing:border-box')
})
