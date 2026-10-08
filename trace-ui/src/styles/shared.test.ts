import {readFileSync} from "node:fs";
import {expect, test} from "vitest";

test('calibration uses darker light mode ink and brighter dark mode ink without a theme control',()=>{
  const css=readFileSync('src/styles/shared.css','utf8')
  const [light,dark]=css.split('@media (prefers-color-scheme: dark)')
  expect(light).toContain('--calibration-final: #15803d')
  expect(light).toContain('--calibration-raw: #1d4ed8')
  expect(dark).toContain('--calibration-final: #4ade80')
  expect(dark).toContain('--calibration-raw: #60a5fa')
})

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
