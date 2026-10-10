import { expect, mock, test } from 'claude-code/testing'
import type { On, SessionAppendInput } from 'claude-code'

const RECORD = '/session/claude/waiter/rewake'
// 2026-10-10 13:05:07 UTC, in epoch seconds
const ARRIVED = Date.UTC(2026, 9, 10, 13, 5, 7) / 1000

const notification: SessionAppendInput = {
  door: 'prompt',
  origin: { kind: 'task-notification' },
  uuid: 'row-1',
  message: { type: 'user', role: 'user', content: [{ type: 'text', text: 'watch wake' }] },
}

const typed: SessionAppendInput = {
  ...notification,
  origin: { kind: 'composer' },
  uuid: 'row-2',
}

// a session whose waiter's record holds `rewake`, absent while undefined, and
// whose clock shows `timeFormat`
function session(on: On, rewake: () => object | undefined, timeFormat = '24-hour-utc'): void {
  mock.env(on, { RIDE_REWAKE_RECORD: RECORD })
  mock.session(on)
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('fs.exists', ($, e) => ({ value: e.path === RECORD && rewake() !== undefined }))
  on('fs.read', ($, e) => ({ value: JSON.stringify(rewake()) }))
  on('config.list', () => ({
    value: [
      {
        key: 'timeFormat',
        label: 'Time format',
        kind: 'choice',
        value: timeFormat,
        options: ['auto', '12-hour', '24-hour', '24-hour-utc'],
        provider: { plugin: 'engine', tier: 'core' },
        isLocked: false,
      },
    ],
  }))
}

function logged(on: On): string[] {
  const lines: string[] = []
  on('ui.log', ($, e) => {
    lines.push(e.text)
    return { value: undefined }
  })
  return lines
}

function line(command: string | null, content: string, wakes = true, arrived = ARRIVED): object {
  return { command, content, wakes, arrived }
}

test('a rewake logs its lines once, at the notification that delivers it', async ($, on) => {
  let rewake: object | undefined = undefined
  session(on, () => rewake)
  const rows = logged(on)
  await $.session.start({ cwd: '/' })

  rewake = {
    count: 1,
    lines: [
      line('quest watch', 'summon started', false),
      line('quest watch', 'summon ended ok', true, ARRIVED + 2),
    ],
    pending: false,
  }
  await $.session.append(typed)
  await $.session.append(notification)
  await $.session.append({ ...notification, uuid: 'row-3' })

  expect(rows).toEqual(['13:05:07Z [quest watch] summon started', '13:05:09Z [quest watch]* summon ended ok'])
})

test('lines left for the next wake close the rows under a blank time', async ($, on) => {
  session(on, () => ({ count: 1, lines: [line('seq', '1')], pending: true }))
  const rows = logged(on)
  await $.session.append(notification)

  expect(rows).toEqual(['13:05:07Z [seq]* 1', '          … more watch lines wait for the next wake'])
})

test('the times follow the clock the session shows', async ($, on) => {
  session(on, () => ({ count: 1, lines: [line('seq', '1')], pending: false }), '12-hour')
  const rows = logged(on)
  await $.session.append(notification)

  expect(rows).toHaveLength(1)
  expect(rows[0]).toMatch(/^\d\d:05:07 [AP]M \[seq\]\* 1$/)
})

test('a rewake recorded before the plugin loaded is not logged again', async ($, on) => {
  session(on, () => ({ count: 2, lines: [line('quest watch', 'ended')], pending: false }))
  const rows = logged(on)
  await $.session.start({ cwd: '/' })

  await $.session.append(notification)

  expect(rows).toEqual([])
})

test('a long command and a long line are cut, their control characters dropped', async ($, on) => {
  const command = `tail -f ${'/deep'.repeat(10)}`
  const content = `\u001b[31mred\u001b[0m ${'x'.repeat(300)}`
  session(on, () => ({ count: 1, lines: [line(command, content)], pending: false }))
  const rows = logged(on)
  await $.session.append(notification)

  expect(rows).toHaveLength(1)
  const [, shown, rest] = rows[0].match(/^13:05:07Z \[(tail -f [^\]]*…)\]\* (.*)$/) ?? []
  expect(shown.length).toBeLessThan(command.length)
  expect(rest.startsWith('red xxx')).toBe(true)
  expect(rest.endsWith('x…')).toBe(true)
})

test("the waiter's own notice is drawn without a tag", async ($, on) => {
  session(on, () => ({ count: 1, lines: [line(null, 'No watch line arrived for 24 hours')], pending: false }))
  const rows = logged(on)
  await $.session.append(notification)

  expect(rows).toEqual(['13:05:07Z No watch line arrived for 24 hours'])
})
