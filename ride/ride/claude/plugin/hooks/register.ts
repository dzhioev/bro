import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

// the rewake record the session's watch waiter keeps
// (`ride.claude.waiter_state.WaiterState.rewake_record`)
// `arrived` in epoch seconds
type RewakeLine = { command: string | null; content: string; wakes: boolean; arrived: number }
type Rewake = { count: number; lines: RewakeLine[]; pending: boolean }

const TAG_CHARACTERS = 30
const CONTENT_CHARACTERS = 200
// an ANSI escape sequence, or any other control character a watched command
// printed, which would draw on the terminal rather than as text
const CONTROL = /\u001b\[[0-?]*[ -/]*[@-~]|[\u0000-\u001f\u007f-\u009f]/g

const shownRewakes = atom({ plugin: 'ride', key: 'shownRewakes' } as const, 0)

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const rewake = await latestRewake($)
    if (rewake !== undefined) {
      await update($, shownRewakes, shown => Math.max(shown, rewake.count))
    }
    return next(e)
  })

  // a rewake reaches the conversation as a task notification, the turn's own
  // prompt or a delivery into a running turn
  on('session.append', async ($, e, next) => {
    if (e.origin.kind === 'task-notification') {
      const rewake = await latestRewake($)
      if (rewake !== undefined && rewake.count > (await read($, shownRewakes))) {
        await update($, shownRewakes, () => rewake.count)
        for (const row of rows(rewake, await clock($))) {
          $.ui.log(row)
        }
      }
    }
    return next(e)
  })
}

async function latestRewake($: EngineInterface): Promise<Rewake | undefined> {
  const path = await $.env.get('RIDE_REWAKE_RECORD')
  if (path === undefined) {
    throw new Error('RIDE_REWAKE_RECORD is unset: the ride plugin runs in a ride session')
  }
  if (!(await $.fs.exists(path))) {
    return undefined
  }
  const record: unknown = JSON.parse(await $.fs.read(path))
  if (!isRewake(record)) {
    throw new Error(`${path} carries a malformed rewake record`)
  }
  return record
}

function isRewake(value: unknown): value is Rewake {
  return (
    typeof value === 'object' &&
    value !== null &&
    'count' in value &&
    Number.isInteger(value.count) &&
    'pending' in value &&
    typeof value.pending === 'boolean' &&
    'lines' in value &&
    Array.isArray(value.lines) &&
    value.lines.every(isRewakeLine)
  )
}

function isRewakeLine(value: unknown): value is RewakeLine {
  return (
    typeof value === 'object' &&
    value !== null &&
    'command' in value &&
    (value.command === null || typeof value.command === 'string') &&
    'content' in value &&
    typeof value.content === 'string' &&
    'wakes' in value &&
    typeof value.wakes === 'boolean' &&
    'arrived' in value &&
    typeof value.arrived === 'number'
  )
}

// a time as Claude Code's own clock shows it under the session's `timeFormat`,
// to the second
async function clock($: EngineInterface): Promise<(milliseconds: number) => string> {
  const setting = (await $.config.list()).find(row => row.key === 'timeFormat')
  if (setting === undefined || typeof setting.value !== 'string') {
    throw new Error('Claude Code lists no timeFormat setting')
  }
  const format = setting.value
  if (format.includes('%')) {
    throw new Error(`the ride plugin draws no strftime timeFormat: ${format}`)
  }
  const options: Intl.DateTimeFormatOptions = { hour: '2-digit', minute: '2-digit', second: '2-digit' }
  if (format === '12-hour') {
    options.hourCycle = 'h12'
  } else if (format === '24-hour' || format === '24-hour-utc') {
    options.hourCycle = 'h23'
  }
  if (format === '24-hour-utc') {
    options.timeZone = 'UTC'
  }
  const formatter = new Intl.DateTimeFormat('en-US', options)
  const suffix = format === '24-hour-utc' ? 'Z' : ''
  return milliseconds => `${formatter.format(milliseconds)}${suffix}`
}

// one row per line: its arrival time, its watch's command shortened and
// marked `*` when the line wakes the session, then what the line says
function rows(rewake: Rewake, time: (milliseconds: number) => string): string[] {
  const lines = rewake.lines.map(line => {
    const tag = line.command === null ? '' : `[${clipped(line.command, TAG_CHARACTERS)}]${line.wakes ? '*' : ''} `
    return `${time(line.arrived * 1000)} ${tag}${clipped(line.content, CONTENT_CHARACTERS)}`
  })
  if (rewake.pending) {
    const blank = ' '.repeat(time(0).length)
    lines.push(`${blank} … more watch lines wait for the next wake`)
  }
  return lines
}

function clipped(text: string, characters: number): string {
  const visible = text.replace(CONTROL, '')
  return visible.length > characters ? `${visible.slice(0, characters)}…` : visible
}
