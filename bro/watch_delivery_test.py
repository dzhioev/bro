from bro import results, watch_delivery, watches
from bro.base.text_window import BYTE_LIMIT


def _lines(command: str, count: int) -> tuple[watches.BatchLine, ...]:
  return tuple(
    watches.BatchLine(command, f'{number:04}' + 'x' * 96, wakes=True, arrived=0.0)
    for number in range(count)
  )


def test_a_batch_within_the_bound_is_delivered_whole_and_kept():
  batch = watches.Batch(_lines('small', 3))
  with results.temporary_store() as store:
    delivery = watch_delivery.deliver(batch, store)

    assert delivery.text == batch.text()
    assert delivery.shown == batch.lines
    assert delivery.kept == ()
    (entry,) = store.entries()
    assert store.read(entry.id)[1] == batch.text()


def test_a_large_watch_is_kept_whole_and_delivered_as_its_head():
  batch = watches.Batch(_lines('flood', 1_000))
  with results.temporary_store() as store:
    delivery = watch_delivery.deliver(batch, store)

    (kept,) = delivery.kept
    entry, text = store.read(kept.result)
    assert text == batch.text()
    assert (entry.tool, entry.arguments) == ('watch', {'command': 'flood'})
    assert delivery.text.startswith(batch.lines[0].text)
    assert f'bro::page(result="{kept.result}", char_offset={BYTE_LIMIT})' in delivery.text
    assert len(delivery.shown) + kept.hidden == 1_000
    assert delivery.text.startswith('\n'.join(line.text for line in delivery.shown))


def test_a_flooding_watch_leaves_a_quiet_one_inline():
  flood = _lines('flood', 1_000)
  quiet = (watches.BatchLine('quiet', 'woke', wakes=True, arrived=0.0),)
  with results.temporary_store() as store:
    delivery = watch_delivery.deliver(watches.Batch(flood + quiet), store)

    assert delivery.text.endswith('\n[quiet] woke')
    assert quiet[0] in delivery.shown
    assert [entry.command for entry in delivery.kept] == ['flood']
    assert len(store.entries()) == 2
