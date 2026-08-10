"""await a blocking call without pinning the event loop or the process exit."""

import asyncio
import contextvars
import threading
from collections.abc import Callable
from typing import Any


async def off_loop[T](function: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
  """run `function` in a daemon thread and await its result.

  `asyncio.to_thread` is the same thing over the default executor, whose threads
  are non-daemon and joined during interpreter shutdown: one call still running
  when the process wants to exit delays the exit by its full remaining runtime,
  cancelled await or not. A daemon thread is never joined, so cancelling the
  await abandons the call and the process exits on schedule — the caller owns
  whatever the abandoned call still holds (a subprocess it started keeps
  running unless someone kills it).
  """
  loop = asyncio.get_running_loop()
  future: asyncio.Future[T] = loop.create_future()
  context = contextvars.copy_context()

  def settle(setter: Callable[[Any], None], value: Any) -> None:
    if not future.cancelled():
      setter(value)

  def run() -> None:
    try:
      result = context.run(function, *args, **kwargs)
    except BaseException as error:
      _post(loop, settle, future.set_exception, error)
    else:
      _post(loop, settle, future.set_result, result)

  threading.Thread(target=run, daemon=True).start()
  return await future


def _post(loop: asyncio.AbstractEventLoop, callback: Callable[..., None], *args: Any) -> None:
  try:
    loop.call_soon_threadsafe(callback, *args)
  except RuntimeError:
    # the loop closed while the abandoned call was still running — its result
    # has no consumer left, which is exactly what abandoning it meant.
    pass
