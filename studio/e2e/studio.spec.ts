import { expect, test } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { TOKEN } from '../playwright.config'

const fixtures = mkdtempSync(join(tmpdir(), 'c2a-fx-'))
const python = process.env.C2A_PYTHON ?? 'python'

test.beforeAll(() => {
  execFileSync(python, ['tests/fixtures/make_fixtures.py', fixtures], { cwd: '..' })
  expect(existsSync(join(fixtures, 'cut.mp4'))).toBe(true)
})

test('doctor lists checks with fix hints', async ({ page }) => {
  await page.goto(`/?token=${TOKEN}`)
  await page.getByTestId('doctor-button').click()
  const doctor = page.getByTestId('doctor')
  await expect(doctor).toContainText('ffmpeg')
  await expect(doctor).toContainText('stack')
})

test('upload → check a key → change style → approve → clips and output play', async ({ page }) => {
  await page.goto(`/?token=${TOKEN}`)
  await page.getByTestId('file-input').setInputFiles(join(fixtures, 'cut.mp4'))

  // key images appear, tagged; samples become approvable
  const checks = page.locator('[data-testid^=key-check-]')
  await expect(checks.first()).toBeVisible()
  await expect(page.locator('[data-testid^=approve-]:not([disabled])').first()).toBeVisible()
  const before = await page.locator('[data-testid^=clip-]').count()

  // check one key image that the tagger left unchecked → one more clip in the plan
  // the checkbox reflects the engine's state, so it flips once the engine confirms
  const unchecked = page.locator('[data-testid^=key-check-]:not(:checked):not([disabled])').first()
  const id = await unchecked.getAttribute('data-testid')
  await unchecked.click()
  await expect(page.getByTestId(id!)).toBeChecked()
  await expect(page.locator('[data-testid^=clip-]')).toHaveCount(before + 1)

  // switch the director's language: samples are re-made for the new style
  await page.getByTestId('style-director').selectOption('ozu_inspired')
  await expect(page.getByTestId('panel-samples')).toContainText('approve one to generate')
  await page.locator('[data-testid^=approve-]:not([disabled])').first().click()

  // clips play inline, the final video plays, the download works
  await expect(page.getByTestId('status')).toContainText('done')
  await page.getByTestId('nav-clips').click()
  await expect(page.locator('[data-testid^=clip-] video').first()).toBeVisible()
  await page.getByTestId('nav-output').click()
  const output = page.getByTestId('output-video')
  await expect(output).toBeVisible()
  const ready = await output.evaluate(
    (v: HTMLVideoElement) =>
      new Promise<number>((r) => {
        if (v.readyState >= 2) return r(v.readyState)
        v.onloadeddata = () => r(v.readyState)
        setTimeout(() => r(v.readyState), 15000)
      }),
  )
  expect(ready).toBeGreaterThanOrEqual(2)
  const href = await page.getByTestId('download').getAttribute('href')
  const res = await page.request.get(href!)
  expect(res.status()).toBe(200)
  expect((await res.body()).length).toBeGreaterThan(10_000)
  await page.screenshot({ path: 'test-results/studio-done.png' })
})

test('the API refuses requests without the token', async ({ request }) => {
  expect((await request.get('/api/runs')).status()).toBe(401)
})
