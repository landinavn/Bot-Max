"""Assertions for the changed log contract; request/result assertions stay intact."""
import json


def assert_event(caplog, source, *, event=None, stage=None, http_status=None):
    rows=[json.loads(r.getMessage()) for r in caplog.records if r.name==source]
    assert rows, f'No structured diagnostic from {source}'
    matching=[r for r in rows if (event is None or r.get('event')==event)
              and (stage is None or r.get('stage')==stage)
              and (http_status is None or r.get('http_status')==http_status)]
    assert matching, (event,stage,http_status,rows)
    for record in caplog.records:
        assert record.args==() and record.exc_info is None and record.exc_text is None
    return matching
