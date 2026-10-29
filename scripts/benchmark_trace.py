"""Bounded local metadata benchmark; no network, business store, or model access."""
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.observability import Runtime, span


def main():
    with tempfile.TemporaryDirectory(prefix='bgm-trace-bench-') as directory:
        runtime=Runtime(directory)
        runtime.start()
        samples=[]
        with runtime.turn('benchmark','synthetic-session','synthetic-request'):
            for _ in range(300):
                start=time.perf_counter()
                with span('context.prepare',stage='synthetic_metadata') as scope:
                    scope.event('context.selection',candidate_count=20,selected_count=10,reason='fixture')
                samples.append((time.perf_counter()-start)*1000)
        started=time.perf_counter()
        runtime.close()
        shutdown_ms=(time.perf_counter()-started)*1000
        result={'iterations':len(samples),'span_and_event_p50_ms':statistics.median(samples),
                'span_and_event_p95_ms':sorted(samples)[int(len(samples)*.95)],
                'shutdown_ms':shutdown_ms,'dropped':runtime.dropped,
                'recording_complete':runtime.query('benchmark','synthetic-session')['recording_complete']}
        print(json.dumps(result,indent=2))
        if result['span_and_event_p95_ms']>2 or shutdown_ms>1000 or runtime.dropped:
            raise SystemExit(1)


if __name__=='__main__':
    main()
